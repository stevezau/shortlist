# Wave 2 frontend-correctness design: `sse-dead`, `four-states`, `contrast`, `legend-bug`

From `.claude/docs/audit-2026-09-programme.md` (Wave 2 — Correctness bugs). Every claim below was
checked against the actual code before being written down; where the audit's framing didn't survive
that check, this doc says so and states what's actually true instead. `pnpm` is not on PATH on this
machine — verification used `cd web && ./node_modules/.bin/tsc -b` / `./node_modules/.bin/vitest run`
directly, and three throwaway repro tests (written, run, and deleted — never committed) to settle the
`legend-bug` whitespace question empirically rather than from memory of JSX's rules.

None of the four items touch the API surface, so **no `pnpm gen:api` is needed for any of them.**

---

## 1. `sse-dead`

### 1.1 What is actually wrong

Three claims, checked against `web/src/lib/sse.ts` and its six call sites
(`activity-pill.tsx`, `runs.tsx`, `jobs.tsx`, `run-detail.tsx`, `uninstall.tsx`,
`step-first-run.tsx`):

**(a) The `connected` flag is genuinely dead — confirmed.** `useSSE` (`sse.ts:38-121`) tracks
`connected` via `useState` and returns `{ connected }`. Checked all six call sites
(`rg -n "useSSE" web/src`): every one calls `useSSE({ ...handlers })` and discards the return value —
none destructures `.connected`. The state exists only to force a re-render on every
open/error/close, for no consumer. `sse.test.ts` tests it (`"reports connected after the stream
opens"`), which is why it reads as intentional rather than an oversight, but the production code
never looks at it.

**(b) No polling fallback when the stream drops — confirmed, and it has already bitten in
production once.** `runs.tsx` carries this comment, dated to a real incident, right next to its
`useSSE` call (`runs.tsx:246-249`):

```
// Live updates. Without this the list is a snapshot: a run that finishes leaves its row reading
// "Running" with a ticking timer for as long as the page stays open, because nothing refetches. On
// a real server that made a cancel that HAD worked look like one that was ignored — the operator
// watches this page, and this page never changed its mind (SFLIX, 2026-08-13).
```

That comment describes the bug this item names — but the current code only fixes the case where
the SSE connection stays open. If the connection itself drops (backgrounded tab, a flaky reverse
proxy, `EventSource`'s own error), the `onerror` handler in `sse.ts:104-110` retries with backoff up
to 30s **forever**, and until it reconnects, `run.finished` is never delivered — so the exact SFLIX
symptom can still recur, just from a different cause. Neither `useRun` (run-detail.tsx's query) nor
`useRunsPaged`/`useRunsSummary` (runs.tsx's) has a `refetchInterval`, and `EventSource` gives no
"replay what I missed" mechanism on reconnect. `jobs.tsx` is the one page that already does this
right (`refetchInterval: (query) => hasActivity ? 3_000 : 15_000`, with its own comment explaining
why "never `false`" matters) — it's the reference pattern for the fix below, not a place needing one.

**(c) "Cancel doesn't invalidate the run query" — checked and it's false as literally stated.**
`useCancelRun` (`queries.ts:321-328`) does call
`queryClient.invalidateQueries({ queryKey: queryKeys.runs })` on success. `queryKeys.run(id)` is
`["runs", id]` — a prefix match of `["runs"]` — and TanStack Query v5's default `invalidateQueries`
matching is a **prefix match**, not an exact-key match. I didn't take that on faith: I ran it against
the actual installed package (`@tanstack/query-core@5.102.8`, the version pinned in
`web/package.json`):

```
$ node qc_test.mjs   # QueryClient.invalidateQueries({queryKey:["runs"]}), then check isStale()
["runs"] isStale: true
["runs",5] isStale: true
["runs","paged",{"collection":"x"}] isStale: true
["users",5,"runs"] isStale: false
```

So cancelling **does** invalidate both the runs list and the specific run's detail query today —
the audit's named mechanism isn't the bug. What's real is the same thing as (b): the invalidation
fires once, synchronously with the cancel _request_ succeeding — which only proves
`cancel_requested` was recorded (cancellation is cooperative, per `08b9c3b`: "the engine checks a
per-run cancel flag before each user... finishes, then bails"), not that the run has actually
stopped. The only thing that later delivers the run's real terminal state is the live
`run.finished`/`onRunUserStage` SSE handlers in `run-detail.tsx:223-234`. If the stream is down when
that happens, `run-detail.tsx` can show "Stopping…" (from `cancel.isSuccess`) with nothing to ever
correct it except a manual reload — the same root cause as (b), not a separate bug. One incidental
finding worth hardening anyway: the invalidation only works because of TanStack's prefix-match
behaviour, which is correct today but not something a future query-key rename would be caught
breaking by any test.

### 1.2 The fix

**(b) and (c) share one fix: a data-driven refetch safety net**, following the exact shape already
proven in `jobs.tsx`, added to `web/src/lib/run-format.ts` (home of the other pure run-derived
helpers — `rankClass`, `errorBucket` — so it's unit-testable without React Query or fake timers):

```ts
// run-format.ts
/** Fallback poll interval, in ms, for a run that hasn't reached a terminal state. Exists because the
 *  ONLY other thing that refreshes an in-flight run is the live SSE stream (run-detail.tsx / runs.tsx
 *  both wire `run.finished`/`onRunUserStage` to invalidateQueries) — and EventSource replays nothing
 *  it missed while disconnected. `false` once a run has `finished_at`, so a settled run is never
 *  re-fetched for no reason. This is what actually prevents the "cancel that had worked looked like
 *  one that was ignored" class of bug (runs.tsx:246-249, SFLIX 2026-08-13) when SSE itself is down,
 *  not just when it's connected but idle. */
const RUN_POLL_FALLBACK_MS = 5_000;

export function runRefetchIntervalMs(
  run: { finished_at: string | null } | undefined,
): number | false {
  return run && !run.finished_at ? RUN_POLL_FALLBACK_MS : false;
}

export function runsListRefetchIntervalMs(
  pages: { finished_at: string | null }[][] | undefined,
): number | false {
  if (!pages) return false;
  return pages.some((page) => page.some((run) => !run.finished_at))
    ? RUN_POLL_FALLBACK_MS
    : false;
}
```

Wired into `queries.ts`:

```ts
// queries.ts
import { runRefetchIntervalMs, runsListRefetchIntervalMs } from "./run-format";

export function useRun(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => api.getRun(id),
    enabled,
    refetchInterval: (query) => runRefetchIntervalMs(query.state.data),
  });
}

export function useRunsPaged(collection?: string) {
  return useInfiniteQuery({
    queryKey: collection
      ? ([...queryKeys.runs, "paged", { collection }] as const)
      : ([...queryKeys.runs, "paged"] as const),
    queryFn: ({ pageParam }) =>
      api.getRuns(collection, pageParam as number | undefined, RUNS_PAGE),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage: Run[]) =>
      lastPage.length < RUNS_PAGE
        ? undefined
        : lastPage[lastPage.length - 1]?.id,
    refetchInterval: (query) =>
      runsListRefetchIntervalMs(query.state.data?.pages),
  });
}
```

**(a):** remove the dead flag rather than invent a use for it — the fix above doesn't need to know
_why_ the last update is stale (dropped connection vs. a missed publish vs. clock skew), only
_whether_ the run is still unfinished, which is strictly more robust than gating on `connected`.
`sse.ts`'s effect drops `useState`/`setConnected` entirely and the hook returns `void`:

```ts
export function useSSE(handlers: SSEHandlers): void {
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  useEffect(() => {
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryMs = INITIAL_RETRY_MS;
    let disposed = false;

    const connect = (): void => {
      if (disposed) return;
      source = new EventSource(eventsUrl());
      source.onopen = () => {
        retryMs = INITIAL_RETRY_MS;
      };
      // ...addEventListener blocks unchanged...
      source.onerror = () => {
        source?.close();
        source = null;
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      source?.close();
    };
  }, []);
}
```

**(c), belt-and-braces only (behaviour doesn't change — proven above):** make the run-detail
invalidation explicit instead of leaning on prefix-match semantics, since a future query-key rename
could silently stop reaching it and nothing would notice:

```ts
export function useCancelRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelRun(id),
    // Explicit on both keys rather than relying on ["runs"] reaching ["runs", id] by prefix match —
    // correct today (verified against @tanstack/query-core directly) but implicit, and a renamed key
    // could silently stop reaching the detail page with nothing to catch it.
    onSuccess: (_data, id) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      queryClient.invalidateQueries({ queryKey: queryKeys.run(id) });
    },
  });
}
```

### 1.3 Tests written first

- `run-format.test.ts` (new cases — **fail today**: `runRefetchIntervalMs`/`runsListRefetchIntervalMs`
  don't exist yet):
  - `test_polls_every_5s_while_a_run_has_no_finished_at`
  - `test_stops_polling_once_finished_at_is_set`
  - `test_treats_an_undefined_run_as_nothing_to_poll`
  - `test_runs_list_polls_if_any_page_has_an_unfinished_run`
  - `test_runs_list_stops_once_every_page_is_finished`
- `sse.test.ts`:
  - **Remove** `"reports connected after the stream opens"` (asserts a property the fix deletes).
  - **Add**, fails today (`result.current` is `{connected: false}`, not `undefined`):
    ```ts
    it("returns nothing — the connection flag was tracked but never read by any page", () => {
      const { result } = renderHook(() => useSSE({}));
      expect(result.current).toBeUndefined();
    });
    ```
  - All other `sse.test.ts` cases are unaffected (they don't touch `.connected`).
- `runs-page.test.tsx`, new — **fails today** (no polling exists, so nothing ever refreshes the row):
  ```ts
  it("still clears a finished run from the list if the SSE connection never delivers run.finished", async () => {
    vi.useFakeTimers();
    const running = {
      id: 2,
      trigger: "manual",
      status: "running",
      started_at: "2026-07-15T04:18:00Z",
      finished_at: null,
      dry_run: false,
      stats: {},
    };
    getRuns.mockResolvedValue([running]);
    renderPage();
    await vi.waitFor(() => expect(screen.getByText(/Running/i)).toBeTruthy());

    // The stream drops and never emits run.finished for this run — the poll is the only thing left.
    getRuns.mockResolvedValue([
      { ...running, status: "aborted", finished_at: "2026-07-15T04:21:00Z" },
    ]);
    await vi.advanceTimersByTimeAsync(5_000);

    expect(await screen.findByText(/aborted/i)).toBeTruthy();
    vi.useRealTimers();
  });
  ```
  Flagged as an open question below: combining fake timers with React Query's async `queryFn` is a
  known source of flakiness in this suite (per the existing "web suite flakiness" history) — the
  pure-function tests above are the primary, reliable regression guard; this page-level test is
  secondary confirmation of the wiring and may need `act()`/timer-advance tuning to land clean.

### 1.4 What could regress, and which test catches it

- **Removing `connected` breaks a caller that reads it.** Grep found none, but the safety net is
  `tsc -b` — a call site destructuring a property a hook no longer returns is a compile error, not a
  silent runtime change. Run `cd web && ./node_modules/.bin/tsc -b` after the edit.
- **Over-polling.** 5s only while a run is genuinely unfinished, and only for runs actually on
  screen; React Query's default `refetchIntervalInBackground: false` already pauses it when the tab
  isn't focused. `runRefetchIntervalMs`/`runsListRefetchIntervalMs`'s "stops once finished" tests
  catch a regression that polls forever.
- **`useCancelRun`'s explicit invalidation duplicating work.** Invalidating the same key twice
  (`queryKeys.runs` then `queryKeys.run(id)`, when the first already covers the second) is a no-op,
  not a bug — React Query dedupes.

---

## 2. `four-states`

### 2.1 What is actually wrong — the real count, not 16

The rule (`rules/frontend.md`): "Every data view handles all four states: loading (skeleton), error
(message + retry), empty (explains why + what to do), success." **The shared pattern the audit asks
to be designed already exists** — `web/src/components/query-boundary.tsx` (`QueryBoundary`,
`EmptyState`, `ErrorState`) implements exactly this, and 24 files already use it correctly
(`login.tsx`, `rows.tsx`, `run-detail.tsx`, `users.tsx`, `settings.tsx`, `watching-account.tsx`,
`pick-outcomes.tsx`, `recent-runs.tsx`, `watch-history.tsx`, `user-row-card.tsx`,
`row-shelf-placement.tsx`, `row-placement-section.tsx`, `dashboard/engagement.tsx`,
`dashboard/impact-report.tsx`, `jobs/activity-feed.tsx`, and more). So there's no new component to
design — the job is finding which views _don't_ use it (or an equivalent) and applying it.

I traced every query-returning hook exported from `queries.ts` (37 of them) to every call site
(`rg` across `src/pages` and `src/components`, ~45 locations) and read each one's actual state
handling — not just whether it imports `QueryBoundary`. Several call sites that looked like
candidates turned out to be deliberately, well-reasoned exceptions, and I've kept those out of the
count with the reasoning, because inflating the number past what's actually wrong isn't more
correct, it's just less useful:

**Not violations (checked and excluded), for the record:**

- `PosterField`'s `useImageProvider` — a capability gate (`aiCapable = provider.data?.capable ?? true`)
  with an explicit "assume capable to avoid a flash" comment, not a data view.
- `row-card.tsx`'s `useLibraries`/`useSettings` — explicitly commented ("null until the library list
  actually arrives — a half-loaded card must not label a row's libraries with raw Plex section
  keys"); the card degrades gracefully and the parent page (`rows.tsx`) already owns the page-level
  loading/error/empty states.
- `row-sources-field.tsx`, `library-picker.tsx`, `cleanup-audit-card.tsx`, `jobs.tsx` (catalog +
  schedule) — all four states genuinely present; `jobs.tsx` even carries a comment describing and
  having already fixed the exact "treating a failed fetch like a slow one" bug I found live
  elsewhere (see `RowEffectivenessPanel` below) — good reference implementations, not violations.
- `owner-note.tsx` — fails open (shows the privacy warning) rather than closed on load/error, which
  is the safer default for a privacy-relevant notice, and is commented as deliberate.
- `requests-settings.tsx`'s `seerrApproves` — explicitly three-valued (`"all" | "none" | "partial" |
null`) so an unresolved fetch renders "nothing is known yet" rather than guessing; a genuinely good
  pattern, not an omission.
- `connections-section.tsx`'s `useRuns` (a "last tested" hint) and `blocked-seeds.tsx`'s
  `useUserHistory` (silently drops its quick-pick list on error, but the manual TMDB search stays
  available) — real gaps, but low-stakes/auxiliary; noted below, not counted in the headline number.

**Confirmed violations (6) — file:line, and which state(s) are actually missing:**

1. **`web/src/components/jobs/row-schedules.tsx:29-30`.** `RowSchedules()` computes
   `groups = (query.data?.rows ?? []).filter(...)` and `if (groups.length === 0) return null;`.
   Loading, a failed fetch, and "genuinely nothing scheduled" are all indistinguishable — all three
   render nothing. **Missing: loading, error.**

2. **`web/src/pages/row-rename.tsx`.** The rename form only renders inside
   `{!confirmed && collection && (...)}` (`row-rename.tsx:176`); `collection` comes from
   `collections.data?.find(...)`. While `useCollections()` is loading, on a failed fetch, or when the
   URL's id matches no real collection, the page shows only its header with a blank area where the
   form should be — no skeleton, no error message, no "this row doesn't exist." **Missing: loading,
   error, not-found (empty).**

3. **`web/src/components/layout/notification-bell.tsx:90-91`.**
   `items = notifications.data?.notifications ?? []; count = items.length;`. A failed fetch and zero
   real notifications both render `count === 0` → "You're all caught up." An error is presented as
   good news. **Missing: loading, error** (and the false-positive "all caught up" is actively
   misleading, not just an omission).

4. **`web/src/components/model-field.tsx`** (shared by `step-curator.tsx:78-82,224-225` and
   `connection-card.tsx:153-162,421-422`). Both callers pass `loading={models.isFetching}` but never
   thread `models.isError` through. A failed model-list fetch (bad key, network) renders identically
   to "loaded, zero models" — "Sensible default" / "Custom…", no indication the fetch failed.
   **Missing: error** (one shared component, two call sites — fixed once).

5. **`web/src/components/settings/api-access-card.tsx:114-152`.** `token = status.data?.token ?? null`,
   `enabled = status.data?.enabled ?? false`. On a failed `useApiToken()` fetch, both default false,
   so the card renders the "Generate token" button as if no token exists — even if one does. Clicking
   it would silently regenerate (and thereby invalidate) the real token with no warning this is a
   replace, not a first creation. Also, loading renders `<p>Loading…</p>`, not a skeleton, contrary to
   the rule's literal "loading (skeleton)". **Missing: error** (and loading isn't a skeleton).

6. **`web/src/components/rows/row-effectiveness.tsx:101`.**
   `{isLoading || !data ? <Skeleton .../> : ...}`. On a failed `useCollectionEffectiveness()` fetch,
   `isLoading` is false and `data` stays `undefined`, so `!data` is true — the panel shows the
   **skeleton forever**, indistinguishable from still loading, with no message and no retry.
   **Missing: error** (present as perpetual loading, which is worse than silent, since it looks like
   something is actively broken but gives no way to tell what).

**The real number is 6, not 16** — audited against the rule as written, with reasoning for every
exclusion above so the count can be checked rather than taken on faith. If the two noted low-stakes
cases (`connections-section.tsx`, `blocked-seeds.tsx`) are counted too under a stricter reading,
that's **8 at most** — still well under the audit's 16, and I found no further candidates after
tracing all 37 query hooks to all their call sites.

### 2.2 The fix — applying the existing pattern, once per violation

**(1) `RowSchedules`** — wrap in the existing `QueryBoundary`; the "nothing scheduled" case moves
inside `children`, where it's reached only once loading/error are ruled out:

```tsx
export function RowSchedules() {
  const query = useSchedule();
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-20 w-full" />}
    >
      {(data) => {
        const groups = (data.rows ?? []).filter((entry) => entry.cron);
        if (groups.length === 0) return null;
        return (
          <section className="space-y-2">{/* ...unchanged body... */}</section>
        );
      }}
    </QueryBoundary>
  );
}
```

**(2) `RowRenamePage`** — wrap the header+form in `QueryBoundary`, with `isEmpty` covering "not
found"; hooks (`useEffect` auto-start, `startRename`, `handleSubmit`) stay exactly where they are —
only the returned JSX changes:

```tsx
return (
  <div className="space-y-6">
    <BackLink to="/rows" label="Back to Rows" />
    <QueryBoundary
      query={collections}
      skeleton={<Skeleton className="h-48 w-full max-w-md" />}
      isEmpty={() => !collection}
      empty={
        <EmptyState
          icon={Pen}
          title="That row doesn’t exist"
          hint="It may have already been deleted, or the link is wrong."
          action={<Button asChild variant="outline"><Link to="/rows">Back to Rows</Link></Button>}
        />
      }
    >
      {() => (
        <>
          <header className="space-y-1">{/* ...unchanged, using `collection`... */}</header>
          {!confirmed && collection && (
            <div className="max-w-md space-y-3">{/* ...unchanged form... */}</div>
          )}
          {running && <ProgressBar done={renamed.length} total={undefined} label="Renaming collections" />}
          {error && <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive-text">{error}</div>}
          {doneEvent && !error && (/* ...unchanged... */)}
        </>
      )}
    </QueryBoundary>
  </div>
);
```

(Needs `Link` added to the existing `react-router` import, and `QueryBoundary`/`EmptyState` imported
from `@/components/query-boundary`.)

**(3) `NotificationBell`** — distinguish "don't know yet" from "zero", and never let a fetch failure
read as good news:

```tsx
const notifications = useNotifications();
const items = notifications.data?.notifications ?? [];
const count = items.length;
// null (not 0) while unresolved, so a failed or loading fetch never shows the bare "0" the
// success-path badge means — undetermined must never be spelled the same as "nothing to see".
const knownCount = notifications.isSuccess ? count : null;
const hasError = items.some((n) => n.severity === "error");
...
<Bell aria-hidden="true" />
{knownCount !== null && knownCount > 0 && ( /* ...unchanged badge... */ )}
...
{notifications.isPending ? (
  <div className="space-y-2 p-3">
    <Skeleton className="h-10 w-full" />
    <Skeleton className="h-10 w-full" />
  </div>
) : notifications.isError ? (
  <div className="p-3">
    <MutationAlert
      error={notifications.error}
      fallback="Couldn’t load notifications."
      onRetry={() => notifications.refetch()}
    />
  </div>
) : count === 0 ? (
  <p className="px-3 py-8 text-center text-sm text-muted-foreground">You&rsquo;re all caught up.</p>
) : (
  <ul className="max-h-96 divide-y overflow-y-auto">{/* ...unchanged... */}</ul>
)}
```

**(4) `ModelField`** — thread the error through once, in the shared component:

```tsx
export function ModelField({ id, value, placeholder, models, loading, error, onChange }: {
  ...
  error?: boolean;
}) {
  ...
  {error && (
    <p role="alert" className="text-xs text-destructive-text">
      Couldn’t load this provider’s model list. You can still type a model id below.
    </p>
  )}
```

Both call sites add `error={models.isError}` alongside their existing `loading={models.isFetching}`.

**(5) `ApiAccessCard`** — a real skeleton, and an explicit error branch, before falling through to
"no token":

```tsx
{status.isPending ? (
  <Skeleton className="h-9 w-full max-w-sm" />
) : status.isError ? (
  <MutationAlert
    error={status.error}
    fallback="Couldn’t check your API token."
    onRetry={() => status.refetch()}
  />
) : enabled && token ? (
  /* ...unchanged token UI... */
) : (
  /* ...unchanged "Generate token" button... */
)}
```

**(6) `RowEffectivenessPanel`** — thread `isError`/`error`/`onRetry` from the parent (`row-editor.tsx`
already has `effectiveness` in scope):

```tsx
// row-editor.tsx
<RowEffectivenessPanel
  data={effectiveness.data}
  isLoading={effectiveness.isLoading}
  isError={effectiveness.isError}
  error={effectiveness.error}
  onRetry={() => void effectiveness.refetch()}
  rowSlug={collection.slug}
/>
```

```tsx
// row-effectiveness.tsx
{isLoading ? (
  <Skeleton className="h-24 w-full" />
) : isError ? (
  <MutationAlert error={error} fallback="Couldn’t load how this row is doing." onRetry={onRetry} />
) : !data ? (
  <Skeleton className="h-24 w-full" />
) : data.first_delivered_at === null ? (
  /* ...unchanged... */
```

### 2.3 Tests written first

- `row-schedules` (new test file or extended `jobs-page.test.tsx`): `shows a skeleton while the
schedule loads`, `shows an error with retry when the schedule fetch fails` — **both fail today**
  (nothing renders in either case; `getByRole("alert")` finds nothing).
- `row-rename.test.tsx` (new): `shows a skeleton while collections load`,
  `shows "That row doesn't exist" for an id with no matching collection`,
  `shows an error with retry when collections fail to load` — **all three fail today** (blank div).
- `notification-bell.test.tsx` (extend existing): `shows a loading skeleton, not a stale zero, while
notifications are loading`, `shows an error with retry instead of "You're all caught up" when the
fetch fails` — **both fail today** (renders "You're all caught up" in both cases).
- `model-field.test.tsx`/`step-curator.test.tsx`/`connection-card.test.tsx`: `shows a message when
the model list fails to load, not a silently empty dropdown` — **fails today**.
- `api-access-card.test.tsx`: `renders a skeleton, not literal "Loading…" text, while checking token
status`, `shows an error with retry instead of the Generate button when the token check fails` —
  **both fail today**.
- `row-effectiveness.test.tsx`: `shows an error with retry instead of an endless skeleton when
effectiveness fails to load` — **fails today** (renders the loading skeleton element with no way to
  distinguish it, and no `role="alert"` appears — a `waitFor` on the alert times out).

### 2.4 What could regress, and which test catches it

- **`RowRenamePage`'s restructure** could break the `autoStart` `useEffect` if `collection` stopped
  being computed before the boundary — it isn't; `collection` stays derived in the component body
  from `collections.data`, only the _rendering_ moves inside `QueryBoundary`'s children. The existing
  auto-start test (if any) plus the new "not found" test both exercise this path.
- **`ModelField`'s new `error` prop is optional** (`error?: boolean`), so every other consumer
  compiles unchanged; `tsc -b` catches a caller that broke.
- **`ApiAccessCard`/`RowEffectivenessPanel` regenerating a token or showing "no data" on a _transient_
  error** rather than genuinely nothing existing — the new tests assert the alert+retry route
  specifically for `isError`, not for a merely-slow load, so a fetch that's just pending still shows
  the skeleton, not the error.

---

## 3. `contrast`

### 3.1 What is actually wrong — verified, file:line

`text-destructive-text` is real: it exists in `web/tailwind.config.ts:24-27`
(`destructive.text: "hsl(var(--destructive-text))"`) and `web/src/index.css:28-35`, where the reason
is spelled out in a comment — `--destructive` (0 72% 51%) measures **3.51–4.02:1 as text**, under
WCAG AA's 4.5:1; `--destructive-text` (0 72% 63%) measures **4.84–5.53:1**, AA-compliant. The
convention is already established: "`text-destructive-text` for words, `bg-destructive` for fills."

Grepping for `text-destructive\b` and excluding the two _correct_ tokens
(`text-destructive-text`, `text-destructive-foreground`) finds **exactly 8** occurrences — matching
the audit's count precisely — all on `<p>` body text rendering an error message:

| #   | File:line                                 | Context                               |
| --- | ----------------------------------------- | ------------------------------------- |
| 1   | `web/src/pages/watching-account.tsx:260`  | `shelfOff.isError` message            |
| 2   | `web/src/pages/watching-account.tsx:726`  | undo-failure message                  |
| 3   | `web/src/pages/watching-account.tsx:857`  | `readHistory.isError` message         |
| 4   | `web/src/pages/watching-account.tsx:998`  | transfer verify-mismatch count        |
| 5   | `web/src/pages/watching-account.tsx:1029` | transfer error message                |
| 6   | `web/src/pages/watching-account.tsx:1092` | undo-failure message (transfer panel) |
| 7   | `web/src/pages/watching-account.tsx:1104` | `transfer.isError` message            |
| 8   | `web/src/components/owner-note.tsx:132`   | `dismiss.isError` message             |

All 8 are exactly the shape the token was built for (words on a surface, not a filled control), so
every one is a straight rename.

### 3.2 The fix

Mechanical `text-destructive` → `text-destructive-text` at the 8 sites above, e.g.:

```diff
- <p className="text-sm text-destructive">
+ <p className="text-sm text-destructive-text">
```

repeated for all 8 (2 use `text-sm`, 5 use `text-xs`, 1 has no size class — the rename is identical
regardless of the accompanying size utility).

### 3.3 Tests written first

This is a visual/contrast fix with no behavioural branch to unit-test meaningfully — the codebase
doesn't have (and doesn't need) a computed-style contrast-ratio test elsewhere either. The concrete,
useful regression guard is a **static one**, matching how the project already treats this kind of
convention:

```ts
// A new case in an existing lint-style test, or a small standalone check:
it("never uses the low-contrast destructive token on body text", () => {
  const offenders = allTsxFiles().flatMap((file) =>
    [...file.content.matchAll(/text-destructive(?!-text|-foreground)\b/g)].map(
      () => file.path,
    ),
  );
  expect(offenders).toEqual([]);
});
```

This **fails today** (lists the 8 files/occurrences above) and passes once they're renamed. It also
catches any _future_ reintroduction, which a one-off rename alone wouldn't.

### 3.4 What could regress, and which test catches it

- **None functionally** — this is a pure class-name swap; both tokens exist and are wired to real CSS
  variables, so there's no risk of an undefined class silently doing nothing.
- **Wrong token chosen** — e.g. accidentally using `text-destructive-foreground` (built for white
  text on a solid `bg-destructive` fill, not for text on the app's normal surfaces) would look
  visually similar but violate the actual contrast reasoning in `index.css`. The static test above
  guards specifically against `text-destructive` (bare), and a manual visual check (dark theme,
  since that's the only theme — `index.css:9-10`) confirms the other two tokens weren't swapped in
  by mistake.

---

## 4. `legend-bug`

### 4.1 What is actually wrong — verified, not assumed

The component is `ResultsLegend` in `web/src/components/runs/user-panel.tsx:268-291` — the "What
changed" key shown above a person's rows on the run detail page. I did not trust the audit's
"stray strikethrough fragment" description; I reproduced it. The suspect code:

```tsx
<span className="inline-flex items-center gap-1.5">
  <span className="line-through">Title</span>
  Rotated out for variety
</span>
```

I rendered this exact JSX with `@testing-library/react` in a throwaway test (written, run, and
deleted — not part of the deliverable) and read the real DOM:

```
FULL textContent: "TitleRotated out for variety"
```

**Confirmed: no space.** JSX collapses the newline-containing whitespace between a closing tag and
following text on the next line to nothing, not to a single space. The same file already knows
about and works around this exact issue elsewhere — `user-panel.tsx`'s real (non-legend) rendering
of rotated-out titles explicitly forces a space before its own `line-through` span:

```tsx
— the row keeps its size, so these made room for the new picks above:{" "}
<span className="line-through">{entry.removed.join(", ")}</span>
```

The legend's copy of the same visual idea was written without that `{" "}`. **The bug is also not
limited to the one instance the audit named** — the very next legend entry has the identical shape
and the identical bug, which I also reproduced and confirmed:

```tsx
<span className="font-semibold tabular-nums text-amber-400">#1–3</span>
Top picks
```

→ `"#1–3Top picks"` (also no space), at `user-panel.tsx:288-289`.

One more thing worth flagging precisely: the **existing test already asserts on this exact text and
passes today**, despite the bug (`run-detail.test.tsx:691-694`,
`expect(screen.getByText("Rotated out for variety")).toBeInTheDocument()`). That's not a
contradiction — Testing Library's `getByText` matches an element's _own direct, non-nested_ text
children, not its full rendered `.textContent`, so it finds the outer `<span>` regardless of what's
glued onto it from a nested child. I confirmed this too: `getByText("Rotated out for variety")`
returns that very outer `<span>`, and _that_ element's `.textContent` is the glued
`"TitleRotated out for variety"`. This is exactly why the regression test below has to assert on
`.textContent`, not repeat the existing `getByText().toBeInTheDocument()` pattern — that pattern is
blind to this whole bug class.

**On "replace its coloured dots with badges"**: checked against `PickLine`
(`user-panel.tsx:118-137`), the component the legend is a key _for_. The real rows use the exact
same visual language the legend does — a plain coloured dot for new/kept
(`h-2 w-2 rounded-full bg-success` / `bg-muted-foreground/30`), literal `line-through` text for
rotated-out titles, and a coloured rank number for top picks — not badges anywhere. Changing only
the _legend_ to badges would make it inconsistent with the content it explains, which is worse, not
better. This is flagged as an open question rather than implemented; see below.

### 4.2 The fix

Add the same `{" "}` the file already uses elsewhere, to both broken entries:

```tsx
function ResultsLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground/70">What changed:</span>
      <span className="inline-flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
        New this run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span
          className="h-2 w-2 rounded-full bg-muted-foreground/30"
          aria-hidden="true"
        />
        Kept from last run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="line-through">Title</span> Rotated out for variety
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="font-semibold tabular-nums text-amber-400">#1–3</span>{" "}
        Top picks
      </span>
    </div>
  );
}
```

I verified the fixed version too, the same way: rendering
`<span className="line-through">Title</span>{" "}` + `Rotated out for variety` on the next line
produces `.textContent === "Title Rotated out for variety"` — exactly one space, matching the
established pattern.

The two dot-based entries need no change: their marker is a decorative, empty
`aria-hidden` span with no text content, so there's nothing for the following label to glue onto —
confirmed by the same reasoning (no text child means no missing-space bug is possible there).

### 4.3 Tests written first

Extend `run-detail.test.tsx`'s existing legend assertions (around line 691) rather than only
repeating the `getByText(...).toBeInTheDocument()` pattern that already passes without catching this:

```ts
// Fails today: today's textContent is "TitleRotated out for variety" (no space).
expect(screen.getByText("Rotated out for variety").textContent).toBe(
  "Title Rotated out for variety",
);
// Fails today: today's textContent is "#1–3Top picks" (no space) — the audit only named the
// strikethrough entry, but the identical bug exists here too.
expect(screen.getByText("Top picks").textContent).toBe("#1–3 Top picks");
```

Both fail against today's code (proven above) and pass once the `{" "}` is added to each entry.

### 4.4 What could regress, and which test catches it

- **None of substance** — this is a two-character addition (`{" "}`) with no logic change; the
  existing `getByText(...).toBeInTheDocument()` assertions keep passing unchanged (the words are all
  still present), and the new `.textContent` assertions are the ones that actually pin the fix.
- **Dots→badges, if done later**: should not be scoped to `ResultsLegend` alone — it would need to
  land in `PickLine`'s actual new/kept dot too, or the legend stops matching what it's a key for.
  That's a real UI redesign, not a "cheap, low risk" Wave 2 fix, and is called out as an open
  question rather than folded into this change.

---

## Open questions

1. **`sse-dead`**: the new page-level fake-timer test (§1.3) combines `vi.useFakeTimers()` with
   React Query's async `refetchInterval`/`queryFn`, a combination this suite has historically found
   flaky (`web-suite-is-flaky-under-load`). The pure-function tests in `run-format.test.ts` are the
   reliable primary guard; worth deciding whether the page-level test is worth keeping if it proves
   fragile in practice, versus relying on the unit tests plus the existing SFLIX-incident test alone.
2. **`sse-dead`**: `useRunsSummary` (`["runs","summary"]`) benefits from the _same_ cascading
   invalidation as `useRun`/`useRunsPaged` today, but wasn't given its own fallback poll — its
   content (aggregate counts, last-run label) is lower-stakes than a single run's live "Stopping…"
   state. Left out to keep this item's blast radius small; flag if the owner wants parity.
3. **`four-states`**: two borderline cases (`connections-section.tsx`'s "last tested" hint,
   `blocked-seeds.tsx`'s `RecentWatchPicker`) were found and are real under a strict reading of the
   rule, but are low-stakes/auxiliary with working fallbacks. Not included in the headline count of 6;
   the owner may want them done anyway for consistency once the six real ones are fixed.
4. **`legend-bug`**: whether "replace coloured dots with badges" should happen at all, and if so,
   whether it should be scoped to _only_ the legend (inconsistent with `PickLine`) or to both the
   legend and the real row rendering (a bigger redesign). Not decided here — flagged for the owner.
5. **`contrast`**: whether the static "no bare `text-destructive` on body text" check (§3.3) is worth
   keeping permanently (e.g. as an ESLint rule or a small standing test) versus being a one-off
   verification for this fix. Not decided here.

## Files/tools read or run for this design (read-only + throwaway verification, no production edits)

`web/src/lib/sse.ts`, `web/src/lib/queries.ts`, `web/src/lib/run-format.ts`, `web/src/lib/types.ts`,
`web/src/pages/runs.tsx`, `web/src/pages/run-detail.tsx`, `web/src/pages/jobs.tsx`,
`web/src/pages/row-rename.tsx`, `web/src/components/layout/activity-pill.tsx`,
`web/src/components/layout/notification-bell.tsx`, `web/src/components/owner-note.tsx`,
`web/src/components/query-boundary.tsx`, `web/src/components/model-field.tsx`,
`web/src/components/settings/api-access-card.tsx`,
`web/src/components/rows/row-effectiveness.tsx`, `web/src/components/rows/row-editor.tsx`,
`web/src/components/rows/row-card.tsx`, `web/src/components/rows/row-sources-field.tsx`,
`web/src/components/rows/library-picker.tsx`, `web/src/components/rows/poster-field.tsx`,
`web/src/components/jobs/row-schedules.tsx`, `web/src/components/settings/cleanup-audit-card.tsx`,
`web/src/components/settings/connections-section.tsx`, `web/src/components/connection-card.tsx`,
`web/src/components/requests-settings.tsx`, `web/src/components/user-detail/blocked-seeds.tsx`,
`web/src/components/runs/user-panel.tsx`, `web/src/components/mutation-alert.tsx`,
`web/src/components/ui/badge.tsx`, `web/tailwind.config.ts`, `web/src/index.css`,
`web/src/test/sse.test.ts`, `web/src/test/runs-page.test.tsx`, `web/src/test/run-detail.test.tsx`,
`web/package.json` (confirmed `@tanstack/react-query@^5.102.8`), `git log -p -- web/src/lib/queries.ts`
(commit `08b9c3b` introducing `useCancelRun`).

Also run: a small Node script against the actual installed `@tanstack/query-core` package to verify
`invalidateQueries` prefix-matching behaviour directly rather than from memory, and three throwaway
`@testing-library/react` repro tests (written, run for their console output, then deleted) to verify
the `legend-bug` whitespace behaviour empirically, both for today's broken code and for the proposed
fix.
