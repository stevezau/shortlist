# Wave 2 backend-correctness design — `false-privacy`, `backend-small`, `secret-key`

From the September 2026 audit — see `.claude/docs/audit-2026-09-programme.md`, Wave 2. Three items,
verified against the code on `dev` before anything here was designed. Where the audit's premise was
imprecise, this document says so and states what is actually true.

**Method.** Every claim below was checked against the source, and every code reference carries a
`file:line`. Nothing here contains a secret, token or key value. All five `backend-small` bugs were
located; the fifth (the off-by-one, §2.4) is real but so small that it is probably not what the audit
meant, and it is reported as such rather than dressed up. Two of the `backend-small` locations were
found twice by independent searches, which is why they are stated with confidence and the others are
not.

---

## 1. `false-privacy` — the page says "already done" before it has looked

### 1.1 What is actually wrong

The audit points at `watching-account.tsx:114`. That line is
`const usersQuery = useUsers();` — the top of `WatchingAccountPage`, where the page's two data
queries are opened. The audit's line reference is the _cause_; the **false claim itself is at
`web/src/pages/watching-account.tsx:191`**:

```tsx
126:   const users = usersQuery.data ?? [];
127:   const collections = collectionsQuery.data ?? [];
131:   const affected = rowsOnTheSharedShelf(collections);
...
188:           body={
189:             affected.length
190:               ? `Rows show on everyone's Home screen only. … Affects ${affected.length} row…`
191:               : "Already done — no row is on the friends' Recommended shelf."
192:           }
```

`useCollections()` (`web/src/lib/queries.ts:365-370`) is a plain `useQuery` with no `initialData` and
no `placeholderData`, so `collectionsQuery.data` is `undefined` while the request is in flight **and**
`undefined` if it fails. Line 127's `?? []` collapses both of those into an empty array, line 131
turns an empty array into "no affected rows", and line 191 renders that as a statement of fact about
the owner's Plex server.

Three separate assertions fall out of one unloaded query:

| What the page shows                                                  | When                          | Truth value |
| -------------------------------------------------------------------- | ----------------------------- | ----------- |
| "Already done — no row is on the friends' Recommended shelf." (191)  | first paint, every time       | unknown     |
| "Do this for me" **disabled** (202: `disabled={!affected.length …}`) | first paint, every time       | unknown     |
| "there are N other people on this server" **omitted** (178-179)      | while `usersQuery` is pending | unknown     |

The error case is worse than the loading case, because it never resolves. A 500 from
`/api/collections`, an expired session, a reverse proxy dropping the request — the page settles
**permanently** on "Already done", with the only control that could fix the problem greyed out and
no error anywhere on screen.

`QueryBoundary` **is** imported in this file (`watching-account.tsx:12`) and **is** used correctly —
but only at line 572, for the transfer step's `useHomeUserCandidates`. Steps 1 and 2 render straight
off the `?? []`. So this is a `rules/frontend.md` violation ("Every data view handles all four
states") in a file that already knows the rule, in the one place where the missing states are a
privacy claim rather than a cosmetic gap.

**Why it matters more than a normal four-states bug.** `.claude/CLAUDE.md` and
`.claude/rules/plex-safety.md` both record that the automatic Privacy Check and its write gate were
**removed at the owner's request on 2026-07-16**. Nothing verifies hiding after the fact any more.
This page is one of the few surfaces that tells the owner anything about their own exposure, so a
default-to-reassuring render is the last check failing open.

### 1.2 Two things the audit did NOT claim, which are also true

Both are honesty problems in the _success_ state. The fix should carry them because it is rewriting
this copy anyway.

1. **"No row is on the friends' Recommended shelf" is a claim about Shortlist's saved settings, not
   about Plex.** `rowsOnTheSharedShelf` (`watching-account.tsx:32-38`) filters `Collection` objects
   from `GET /api/collections`, whose `placement_friends` field (`CollectionOut`,
   `shortlist/server/api/collections.py:363`) is stored intent. Nothing anywhere reports what is
   actually on a Plex shelf right now — `shortlist/server/api/watching_account.py` exposes only
   `/candidates`, `/snapshots`, `/transfer`, `/undo`. The server-side twin of this check,
   `notifications._owner_sees_all_rows` (`shortlist/server/notifications.py:405-414`), queries the
   same `Collection.placement_friends` column, so page and bell agree — they are just both
   describing configuration.
2. **The page already knows this and says so in one place only.** After the action succeeds, lines
   250-257 render "Plex still shows the rows until each one is rebuilt — placement is applied by a
   row's next run, not straight away." The `"Already done"` branch has no such caveat, so a row whose
   `placement_friends` was cleared five minutes ago reads as "already done" while it is still on the
   shelf. `it("says the shelf only clears on each row's next run")`
   (`web/src/test/watching-account.test.tsx:128`) exists precisely because "a green tick with no
   timing reads as 'your shelf is clear now', which it is not."

### 1.3 The four states, and what each should say

Named states, not ad-hoc ternaries — the point of the fix is that "I have not looked" stops being
spelled the same way as "I looked and it is fine".

| State        | Condition                    | Body copy                                                                                                                                               | Action                                                                      |
| ------------ | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **checking** | `collectionsQuery.isPending` | "Checking which of your rows are on the shelf…"                                                                                                         | skeleton; button disabled, labelled "Checking…"                             |
| **unknown**  | `collectionsQuery.isError`   | "Shortlist couldn't read your rows, so it can't tell you whether any are on the friends' Recommended shelf."                                            | **Try again** (refetch); the "Do this for me" button is not rendered at all |
| **exposed**  | loaded, `rows.length > 0`    | existing copy + count                                                                                                                                   | **Do this for me**, enabled                                                 |
| **clear**    | loaded, `rows.length === 0`  | "No row is set to appear on the friends' Recommended shelf. Plex catches up on each row's next run, so one you turned off just now may still be there." | button disabled, labelled "Nothing to change"                               |

A fifth condition collapses into **clear** with different copy, because the action is the same
(nothing to do) but the reason is different and the owner will otherwise wonder if the page is
broken: **no per-person rows exist at all**. `rowsOnTheSharedShelf` filters on
`build === "per_person"`, so a server with only shared rows lands in `clear`, and reading "no row is
set to appear" invites "…but I have five rows". Copy: "You have no per-person rows, so nothing stacks
up on your shelf."

### 1.4 The fix

Add an exported state function beside `rowsOnTheSharedShelf`, so the four states are unit-testable
without rendering:

```tsx
/** What the page actually KNOWS about the owner's shelf, as four distinct answers.
 *
 *  Split out because `collectionsQuery.data ?? []` spelled "still loading" and "the request failed"
 *  exactly like "I checked and there is nothing there" — and this page's job is to tell the owner
 *  about their own exposure. The automatic Privacy Check that used to verify hiding after each write
 *  was removed on 2026-07-16 (plex-safety.md), so a default-to-reassuring render here is the last
 *  check failing open, not a cosmetic loading gap.
 *
 *  `clear` is a claim about Shortlist's SAVED placement, not about Plex: `placement_friends` is
 *  applied by a row's next run, and no endpoint reports what is on a shelf right now. The copy for
 *  that state says so out loud. */
export type ShelfState =
  | { kind: "checking" }
  | { kind: "unknown"; retry: () => void }
  | { kind: "clear"; anyPerPersonRows: boolean }
  | { kind: "exposed"; rows: Collection[] };

export function shelfState(query: UseQueryResult<Collection[]>): ShelfState {
  if (query.isPending) return { kind: "checking" };
  if (query.isError)
    return { kind: "unknown", retry: () => void query.refetch() };
  const rows = rowsOnTheSharedShelf(query.data);
  if (rows.length) return { kind: "exposed", rows };
  return {
    kind: "clear",
    anyPerPersonRows: query.data.some(
      (r) => r.enabled && r.build === "per_person",
    ),
  };
}
```

In `WatchingAccountPage`, replace lines 127 and 131:

```tsx
const shelf = shelfState(collectionsQuery);
// Still derived, for the mutation and the partial-failure line. Safe: the mutation can only fire
// from the `exposed` branch, which is the only one that renders an enabled button.
const affected = shelf.kind === "exposed" ? shelf.rows : [];
```

and the OptionCard's `body` / `action` (lines 186-215). `body` is already typed `React.ReactNode`
(`watching-account.tsx:79`), so a skeleton can go straight in:

```tsx
<OptionCard
  title="Take the rows off the library shelf"
  body={
    shelf.kind === "checking" ? (
      <Skeleton className="h-4 w-64" />
    ) : shelf.kind === "unknown" ? (
      // NOT "already done". This branch is the whole point of the change: a failed read must
      // never render as a clean bill of health on the one page that talks about exposure.
      "Shortlist couldn't read your rows, so it can't tell you whether any are on the friends' Recommended shelf."
    ) : shelf.kind === "exposed" ? (
      `Rows show on everyone's Home screen only. Nobody sees anyone else's, including you. You lose the row inside Movies and TV Shows. Affects ${shelf.rows.length} row${shelf.rows.length === 1 ? "" : "s"}.`
    ) : shelf.anyPerPersonRows ? (
      "No row is set to appear on the friends' Recommended shelf. Plex catches up on each row's next run, so one you turned off just now may still be there."
    ) : (
      "You have no per-person rows, so nothing stacks up on your shelf."
    )
  }
  action={
    shelf.kind === "unknown" ? (
      <Button variant="outline" onClick={shelf.retry}>
        <RefreshCw aria-hidden="true" />
        Try again
      </Button>
    ) : chose === "shelf-off" ? (
      <span className="flex items-center gap-1.5 text-sm text-success">
        <Check className="h-4 w-4" aria-hidden="true" />
        Saved
      </span>
    ) : (
      <Button
        variant="outline"
        disabled={shelf.kind !== "exposed" || shelfOff.isPending}
        onClick={() => shelfOff.mutate()}
      >
        {shelfOff.isPending && (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        )}
        {shelf.kind === "checking"
          ? "Checking…"
          : shelf.kind === "clear"
            ? "Nothing to change"
            : "Do this for me"}
      </Button>
    )
  }
/>
```

And the Step 1 sentence at lines 178-179, which currently _omits_ the roster clause while
`usersQuery` is pending rather than claiming anything false. Lower stakes, one line, same principle:

```tsx
{
  usersQuery.isSuccess &&
    others.length > 0 &&
    ` — there are ${others.length} other ${others.length === 1 ? "person" : "people"} on this server`;
}
```

`isSuccess` rather than `others.length > 0` alone, so the clause is absent because the answer is
"none" — never because the answer has not arrived. Those are indistinguishable today.

### 1.5 Tests, written first

`web/src/test/watching-account.test.tsx`. `renderPage()` already builds a `QueryClient` with
`retry: false`, so a rejected mock lands in `isError` on the first tick.

**Fail against today's code:**

```tsx
it("does not say the shelf is clear before the rows have loaded", async () => {
  // The bug in one line: `collectionsQuery.data ?? []` spells "still loading" exactly like "nothing
  // there", so the first paint asserted a fact about the owner's Plex server that no request had
  // answered yet.
  listCollections.mockReturnValue(new Promise(() => {})); // never resolves
  renderPage();

  expect(screen.queryByText(/already done/i)).not.toBeInTheDocument();
  expect(
    screen.queryByText(/no row is set to appear/i),
  ).not.toBeInTheDocument();
  expect(
    await screen.findByRole("button", { name: /checking/i }),
  ).toBeDisabled();
});

it("says it could not check, not that the shelf is clear, when the rows fail to load", async () => {
  // The error case never resolves on its own: today the page settles PERMANENTLY on "already done"
  // with the fix greyed out and nothing on screen saying a request failed.
  listCollections.mockRejectedValue(new Error("boom"));
  renderPage();

  expect(
    await screen.findByText(/couldn.t read your rows/i),
  ).toBeInTheDocument();
  expect(screen.queryByText(/already done/i)).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /try again/i }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /do this for me/i }),
  ).not.toBeInTheDocument();
});

it("does not claim a person count before the roster has loaded", async () => {
  getUsers.mockReturnValue(new Promise(() => {}));
  renderPage();

  expect(
    screen.queryByText(/other people on this server/i),
  ).not.toBeInTheDocument();
});
```

**Pass today, guard the fix** (a `shelfState` unit test, no rendering):

```tsx
describe("shelfState", () => {
  it("tells the four answers apart", () => {
    expect(shelfState({ isPending: true } as never).kind).toBe("checking");
    expect(
      shelfState({ isPending: false, isError: true, refetch: vi.fn() } as never)
        .kind,
    ).toBe("unknown");
    expect(
      shelfState({ isPending: false, isError: false, data: [row({})] } as never)
        .kind,
    ).toBe("exposed");
    expect(
      shelfState({
        isPending: false,
        isError: false,
        data: [row({ placement_friends: "home" })],
      } as never).kind,
    ).toBe("clear");
  });

  it("distinguishes a clear shelf from a server with no per-person rows at all", () => {
    // Same action (nothing to do), different reason. Merged, the copy reads as "no row is set to
    // appear" to someone looking at five rows on the Rows page.
    const shared = shelfState({
      isPending: false,
      isError: false,
      data: [row({ build: "shared" })],
    } as never);
    expect(shared).toEqual({ kind: "clear", anyPerPersonRows: false });
  });
});
```

**One existing test must be rewritten, and it is the reason this bug survived.**
`it("offers nothing to do when no row is on the friends' shelf")`
(`web/src/test/watching-account.test.tsx:184-193`) asserts that
`findByRole("button", {name: /do this for me/i})` is disabled and `getByText(/already done/i)` is
present. Both are true **on the first paint, before `listCollections` resolves** — the button is
always rendered, and it is disabled by the very `?? []` this design removes. The test therefore
passes against a mock that never resolves at all, which _is_ the broken state. This is the
`absence-assertions-go-green-too-early` shape. Replace its body with an assertion that has to wait:

```tsx
it("says the shelf is clear only once the rows have actually loaded", async () => {
  listCollections.mockResolvedValue([row({ placement_friends: "home" })]);
  renderPage();

  // `findByText`, not `getByText`: the pre-load render must NOT contain this sentence, so the
  // assertion has to wait for it rather than find it immediately. That is the whole difference
  // between this test and the one it replaces.
  expect(
    await screen.findByText(/no row is set to appear/i),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /nothing to change/i }),
  ).toBeDisabled();
});
```

**Proving the teeth.** The never-resolving-promise test is its own positive control: point
`listCollections` at a promise that never settles and today's page still renders "Already done".

### 1.6 What could regress, and which test catches it

| Regression                                                                                                                   | Caught by                                                                                                                                     |
| ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| The action fires against a stale/empty `affected` because the mutation reads a different source than the button's enablement | `takes rows off the friends' shelf while leaving Home placement alone` (existing — asserts the exact PATCH kwargs, per `rules/testing.md`)    |
| The partial-failure line ("Changed 1 of 2") loses its denominator now that `affected` is derived from the state              | `says how many rows were saved before a failure, not that nothing changed` (existing)                                                         |
| The error branch swallows the whole card and the owner loses the other two options                                           | The other two `OptionCard`s are outside this branch; `test_the_guide_renders_all_three_options` (`tests/e2e/test_watching_account_e2e.py:21`) |
| `clear` and "no per-person rows" copy get merged back into one string                                                        | `distinguishes a clear shelf from a server with no per-person rows at all`                                                                    |
| The Step 1 roster clause is gated on the wrong query                                                                         | `does not claim a person count before the roster has loaded`                                                                                  |

### 1.7 Settings / migration / API / UI impact

None to settings, the schema, or the API. UI-only: one page file plus its test file. `TransferSteps`
is untouched, so the setup wizard that mounts it is unaffected. No Architecture Review required by
`.claude/CLAUDE.md`'s risk list (UI-only, no Plex write, no migration, no auth) — though the copy is
about privacy and deserves a careful read.

Verification: `pnpm -C web test`, `tsc -b` (per the memory note — `tsc --noEmit` misses the test
files and CI will not), `pnpm -C web build`, and `pytest -m e2e` because a UI flow changed.

### 1.8 Open questions

- **Should `clear` be provable rather than inferred?** Nothing today reports what is actually on a
  Plex shelf. `notifications._shelf_contention` and `_rows_we_cannot_hide` already reason about real
  shelf state from run output, so a `GET /api/watching-account/shelf` answering from the last run's
  delivery ledger is buildable. Out of scope here; the copy in §1.3 is written to be true without it.
- **Should the whole page block on `collectionsQuery`?** This design scopes the boundary to the one
  card, so Step 1's explanation (which needs no data) still renders during a Plex outage. If the
  owner prefers a single page-level skeleton, that is a one-line change to wrap Step 2.

---

## 2. `backend-small` — five bugs

The audit gives one line: _"5 bugs: logout CSRF, unredacted debug log, 2 missing try/except,
off-by-one bound"_, with no file references anywhere in `audit-2026-09-programme.md` and no matching
entries in `.claude/docs/review-backlog.md`. Each was located independently.

| Bug                       | Located          | Where                                                                                                         |
| ------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------- |
| (a) logout CSRF           | yes              | `shortlist/server/auth.py:493-498`                                                                            |
| (b) unredacted debug log  | yes              | `shortlist/server/api/support.py:483`                                                                         |
| (c) missing try/except ×2 | yes              | `shortlist/server/auth.py:349-358` and `:373-391` (a third candidate, `services/backup.py:69-78`, is in §2.3) |
| (d) off-by-one bound      | yes, but trivial | `shortlist/server/auth.py:263` + `:286`                                                                       |

### 2.1 (a) Logout has no CSRF check

```
shortlist/server/auth.py:493  @router.post("/logout", response_model=LogoutOut)
shortlist/server/auth.py:495  async def logout(request: Request, response: Response) -> dict:
shortlist/server/auth.py:497      response.delete_cookie(SESSION_COOKIE, path=_cookie_path(request))
shortlist/server/auth.py:498      return {"ok": True}
```

No `Depends`, no `_check_csrf(request)`. Every other mutation in the app goes through
`require_owner` (`auth.py:270`) or `require_setup_access` (`auth.py:300`), and both call
`_check_csrf` (`auth.py:236-239`). The logout route calls neither — it is the one mutation in the
app that opted out of both gates. Any page on any origin can `fetch("/api/auth/logout", {method:
"POST", credentials: "include"})`, or submit a plain HTML form (which needs no CORS preflight), and
end the owner's session. Low severity — it destroys a session rather than creating one — but it is a
stated invariant of this file, broken in one handler.

**The fix is `_check_csrf`, not `require_owner`.** Logout must stay callable by a session that is
_not_ the owner's — one issued during the pre-link window, or one belonging to an account that has
since lost ownership (`require_owner`'s docstring: "a session issued during the pre-link window
loses all access the moment a different account links a server") — and by a caller holding nothing
but a stale cookie. Requiring ownership would strand exactly the sessions most in need of ending.

```python
@router.post("/logout", response_model=LogoutOut)
async def logout(request: Request, response: Response) -> dict:
    # CSRF only, deliberately NOT `require_owner`: a session that is not (or is no longer) the
    # owner's must still be able to end itself, and so must a caller holding nothing but a stale
    # cookie. The header is the whole check — the SPA already sends it on every mutation
    # (`web/src/lib/api.ts:164-165`), so this is invisible to the app while closing the cross-origin
    # form POST that could sign the owner out from any page they happened to visit. This was the one
    # mutation in the app that reached neither `require_owner` nor `require_setup_access`.
    _check_csrf(request)
    # Same path as the one it was set with, or the browser keeps the cookie and logout does nothing.
    response.delete_cookie(SESSION_COOKIE, path=_cookie_path(request))
    return {"ok": True}
```

**No existing test breaks.** `tests/integration/conftest.py:55` sets
`test_client.headers[CSRF_HEADER] = "1"` on the shared `client` fixture, so
`test_logout_confirms_and_clears_the_cookie` (`tests/integration/test_api_auth_setup.py:128-132`)
already sends the header; `tests/unit/test_base_path_app.py:155` passes it explicitly. Both keep
passing — verified before designing the fix, because a green-to-red flip there would have been the
first thing to notice.

Tests first (the first fails today — today it returns 200):

```python
def test_logout_without_the_csrf_header_is_refused(self, client: TestClient):
    """A cross-origin form POST carries the session cookie and no custom header. Without this check
    any page the owner visited could sign them out."""
    del client.headers[CSRF_HEADER]

    r = client.post("/api/auth/logout")

    assert r.status_code == 403
    assert CSRF_HEADER in r.json()["detail"]
    # A refused request must not half-perform the action.
    assert client.cookies.get(SESSION_COOKIE)


def test_logout_still_works_for_a_session_that_is_not_the_owner(self, client: TestClient):
    """`_check_csrf`, not `require_owner`: a pre-link session, or one whose account lost ownership,
    must still be able to end itself — that is the session most in need of ending."""
    client.cookies.set(
        SESSION_COOKIE,
        session_serializer(client.app.state.session_secret).dumps({"account_id": 999999, "username": "someone-else"}),
    )

    assert client.post("/api/auth/logout").json() == {"ok": True}
```

### 2.2 (b) An unredacted DEBUG log — a genuine rule 9 violation

`shortlist/server/api/support.py:473-484`:

```python
def _check(name: str, fn) -> dict:
    try:
        ok, detail = fn()
        return {"name": name, "ok": bool(ok), "detail": str(detail)}
    except Exception as e:
        logger.debug("support health probe {} failed: {}", name, e)  # <-- RAW
        return {"name": name, "ok": False, "detail": _fail(e)}  # <-- scrubbed
```

**The same exception is scrubbed for the JSON body and logged raw, two lines apart, inside one
`except` block.** `_fail` (`support.py:295-302`) is documented as _"One exception-to-string
conversion for the whole module, scrubbed… the single choke point every such string passes
through"_, and it returns `_scrub(f"{type(e).__name__}: {e}")`. The `logger.debug` bypasses it.

**The exposure is real, not theoretical, and this codebase has already written it down.**
`shortlist/engine/pipeline.py:1568-1573`, guarding the identical hazard:

> `redact` because this is plexapi error text (rule 9). plexapi raises
> `f'({status}) {codename}; {response.url} {errtext}'`, and `response.url` carries the X-Plex-Token
> whenever `log.show_secrets` is on — which `PlexConfig.get` reads from the environment first, so
> `PLEXAPI_LOG_SHOW_SECRETS=true` on the container turns this into a token in the logs without
> touching our code. Safe by default is not the same as guarded.

`_check` wraps eight probes (`support.py:508-528`), two of which make live plexapi calls:
`_probe_libraries` (`support.py:541-545`) calls `client.sections()`, and `_probe_plex`
(`support.py:530-538`) reads `client._server`. A `BadRequest`/`NotFound` out of `sections()` is
exactly the shape the comment above describes, so the path is reachable, not hypothetical.

Three things make it worse than a stray debug line:

1. **`/config/logs/shortlist.log` is written at DEBUG regardless of the console level**
   (`shortlist/logging_config.py:84-85`: `logger.add(_log_file, level="DEBUG", …)`). A `logger.debug`
   is _always_ persisted, even on an install whose `log.level` is INFO.
2. **The support page is where an owner goes when Plex is misbehaving** — i.e. exactly when these
   probes throw. It is a failure-triggered log line, so it fires in the situation it is worst in.
3. The support **bundle** export would redact it on the way out (`log_reader.scrub` =
   `http_retry.redact`, whose `_SECRET_RE` matches `X-Plex-Token=`), so this would not reach a public
   issue tracker. That is a mitigation, not a defence: rule 9 says _never logged_, and the raw value
   still sits in the file on disk and in anything that reads the file directly.

**Fix** — compute the scrubbed string once and log that. It also removes the double formatting:

```python
    except Exception as e:
        # `_fail` scrubs this exception for the JSON body; the log line did not, so the ONE exception
        # in this block reached the durable log raw. plexapi formats its errors as
        # `f'({status}) {codename}; {response.url} {errtext}'` and `response.url` carries the
        # X-Plex-Token whenever `PLEXAPI_LOG_SHOW_SECRETS=true` is set on the container —
        # `PlexConfig.get` reads that from the environment, so no Shortlist code has to be wrong for
        # it to leak (pipeline.py:1568-1573 documents the same mechanism). `_probe_libraries` calls
        # `client.sections()`, a live plexapi call, so this is reachable — and the file sink under
        # /config/logs is DEBUG regardless of the console level, so a debug line here is always kept.
        detail = _fail(e)
        logger.debug("support health probe {} failed: {}", name, detail)
        return {"name": name, "ok": False, "detail": detail}
```

**Test first** (fails today). Uses an obviously-fake sentinel, never a real credential:

```python
def test_a_failed_probe_is_scrubbed_before_it_reaches_the_log(self, client, caplog_loguru):
    """FAILS TODAY. `_fail` scrubbed the JSON and the log line did not, in the same `except` block —
    and plexapi's error text carries the request URL, X-Plex-Token included, whenever
    PLEXAPI_LOG_SHOW_SECRETS is set on the container."""
    sentinel = "NOT-A-REAL-TOKEN-0000"
    boom = RuntimeError(f"(401) unauthorized; http://pms:32400/library/sections?X-Plex-Token={sentinel}")

    result = support._check("Libraries", lambda: (_ for _ in ()).throw(boom))

    assert sentinel not in result["detail"]  # already true today
    assert sentinel not in captured_log_text()  # FALSE today — this is the bug
    assert "<redacted>" in captured_log_text()
```

`tests/unit/test_redaction.py` already exercises `_scrub`/`redact` directly, so the sibling
assertion (`_SECRET_PATTERN` catches the `X-Plex-Token=` form) is covered; this test's job is
purely that the log line _goes through_ the scrubber. There is no loguru capture fixture in
`tests/conftest.py` today — add one (`logger.add(sink, level="DEBUG")` around the call, removed in
teardown) or use `caplog` via the stdlib bridge `configure_logging._bridge_stdlib_logging` installs.

**Adjacent finding, same rule, worth its own line.** `shortlist/server/services/run_service.py:357-362`
is the outermost catch-all around `engine_run` — the whole nightly pipeline:

```python
            except Exception as e:
                logger.exception("run {} failed", run_id)
                self._mark_run_error(run_id, {"error": f"{type(e).__name__}: {e}"})
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error",
                                                   "error": f"{type(e).__name__}: {e}"})
```

The raw `str(e)` fans out three ways: `logger.exception` (which prints the exception line in the
traceback — `backtrace=False, diagnose=False` in `logging_config.py:83-85` strips locals but not the
message), a persisted `runs` row later surfaced by the run-detail API, and an SSE broadcast to every
connected browser. Individual PMS/plex.tv call sites inside `engine_run` are deliberately wrapped in
`redact(str(e))`, so this catch-all is a backstop for un-audited paths — but a backstop is exactly
where an un-audited path arrives, and this one is not redacted. `redact()` is already imported
throughout `services/`; wrapping both `f"{type(e).__name__}: {redact(str(e))}"` and using
`logger.error(..., redact(str(e)))` in place of `logger.exception` is a two-line change.

Whether that belongs in `backend-small` or its own item is the owner's call; it is listed here
because it was found while verifying (b) and is the same rule.

### 2.3 (c) Two missing try/except — both in the login handshake

Both are in `auth.py`, and both contradict a pattern the **same file** documents fifty lines
earlier. `owned_machine_ids` (`auth.py:100-150`):

> Raises `httpx.HTTPError` on ANY failure to get a usable answer — transport, status, or a body that
> is not the list of resources we expect. … a captive portal or proxy answering `200 text/html` used
> to surface as an unhandled 500 with nothing in the log.

That hardening was applied to `owned_machine_ids` and to `_seeded_token_account_id`
(`auth.py:183-205`, which turns an unreachable plex.tv into a 503 with real copy) and to nothing else
in the file.

**Site 1 — `create_pin`, `auth.py:349-358`:**

```python
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{PLEXTV}/api/v2/pins", params={"strong": "true"}, …)
    r.raise_for_status()
    data = r.json()
    return {"id": data["id"], "code": data["code"], "client_id": request.app.state.client_id}
```

Four unhandled failure modes, all reachable from an ordinary home network: `client.post` raises
`httpx.ConnectError`/`ConnectTimeout` when plex.tv is unreachable (a state this deployment has
actually seen — memory note `plextv-404s-transiently.md`); `raise_for_status()` raises
`httpx.HTTPStatusError` on a plex.tv 5xx; `r.json()` raises `ValueError` on the `200 text/html`
captive-portal body the file already names; `data["id"]` raises `KeyError` on a JSON body of the
wrong shape. Each becomes an unhandled 500. This is the **first** call the login screen makes, so
"plex.tv is down" and "Shortlist is broken" are indistinguishable to the owner.

**Site 2 — `poll_pin`, `auth.py:373-391`:**

```python
        r = await client.get(f"{PLEXTV}/api/v2/pins/{pin_id}", …)
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="PIN expired — start over")
        r.raise_for_status()
        token = r.json().get("authToken")
        …
    account.raise_for_status()
    info = account.json()
    account_id = int(info["id"])
```

The same four, plus `int(info["id"])` raising `ValueError`/`TypeError` on a body that is JSON but not
the expected shape. The SPA polls this roughly every 1.5s while the owner authorises in Plex
(`auth.py:79-82`), so one transient plex.tv blip produces a burst of 500s and a login that fails
with no reason given.

**Fix** — one helper used at both sites, mirroring `_seeded_token_account_id`'s 503:

```python
_PLEXTV_UNREACHABLE = "could not reach plex.tv — try again in a moment"


def _plextv_body(response: httpx.Response, what: str) -> dict:
    """A plex.tv JSON object, or an HTTPException that says what actually went wrong.

    Every other plex.tv caller in this file already fails this way (`owned_machine_ids`,
    `_seeded_token_account_id`); the two login handlers did not, so an unreachable plex.tv, a 5xx, or
    the `200 text/html` a captive portal returns each surfaced as an unhandled 500 — during the login
    handshake, the one flow with no other way to report anything.
    """
    if response.status_code >= 400:
        logger.warning("{}: plex.tv returned HTTP {}", what, response.status_code)
        raise HTTPException(status_code=502, detail=f"plex.tv returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as e:  # HTML/XML from a portal or proxy, not JSON
        logger.warning("{}: plex.tv returned a non-JSON body", what)
        raise HTTPException(status_code=502, detail="plex.tv did not answer with a PIN") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="plex.tv returned an unexpected payload")
    return body
```

`create_pin` becomes:

```python
@router.post("/pin", response_model=PinOut)
async def create_pin(request: Request) -> dict:
    _rate_limit_pin(request)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{PLEXTV}/api/v2/pins",
                params={"strong": "true"},
                headers=_client_headers(request.app.state.client_id),
                timeout=15,
            )
    except httpx.HTTPError as e:
        # Only the class name — an httpx error can carry the request URL, and rule 9 keeps
        # credential-shaped text out of log lines by default rather than by inspection.
        logger.warning("create pin: plex.tv unreachable ({})", type(e).__name__)
        raise HTTPException(status_code=503, detail=_PLEXTV_UNREACHABLE) from e
    data = _plextv_body(r, "create pin")
    if not isinstance(data.get("id"), int) or not data.get("code"):
        raise HTTPException(status_code=502, detail="plex.tv returned a PIN with no id or code")
    return {"id": data["id"], "code": data["code"], "client_id": request.app.state.client_id}
```

`poll_pin` keeps its 404 branch (a real, meaningful answer that must survive the refactor) and wraps
the rest:

```python
try:
    async with httpx.AsyncClient() as client:
        pin = await client.get(f"{PLEXTV}/api/v2/pins/{pin_id}", headers=_client_headers(state.client_id), timeout=15)
        if pin.status_code == 404:
            raise HTTPException(status_code=404, detail="PIN expired — start over")
        token = _plextv_body(pin, "poll pin").get("authToken")
        if not token:
            return {"linked": False}
        account = await client.get(
            f"{PLEXTV}/api/v2/user", headers={**_client_headers(state.client_id), "X-Plex-Token": token}, timeout=15
        )
except httpx.HTTPError as e:
    logger.warning("poll pin: plex.tv unreachable ({})", type(e).__name__)
    raise HTTPException(status_code=503, detail=_PLEXTV_UNREACHABLE) from e
info = _plextv_body(account, "identify account")
try:
    account_id = int(info["id"])
except (KeyError, TypeError, ValueError) as e:
    # A body we can't read is not an answer — the same rule `_seeded_token_account_id` applies.
    raise HTTPException(status_code=502, detail="plex.tv did not say which account approved this PIN") from e
```

`HTTPException` is not an `httpx.HTTPError`, so the 404 raised inside the `try` passes through the
`except` untouched. Worth a comment in the code, because it looks wrong at a glance.

**A third candidate, if the audit's "two" are not these two.** `services/backup.py:69` calls
`_rotate` **outside** `take_backup`'s `try/except` — the guarded block ends with the `finally` at
lines 61-67 — and `_rotate` itself has no handler:

```
backup.py:75      backups = sorted(backup_dir.glob("shortlist_*.db"), key=lambda p: p.stat().st_mtime, …)
backup.py:77          old.unlink()
```

Both lines can raise. `p.stat()` raises `FileNotFoundError` if a file vanishes between the `glob`
and the sort — the scheduled backup job and the boot-time one racing each other is enough — and
`old.unlink()` raises on a permission problem or a locked file, which is not exotic on the NAS and
Unraid mounts this app targets. The call chain makes that fatal: `db/session.py:163` calls
`take_backup(config_dir, label="pre-migration")` unguarded inside `run_migrations`, and
`main.py:132` makes `run_migrations(config_dir)` the **first statement of `lifespan`**. So a failure
while deleting an old backup takes the container down, and it takes it down on the one boot where a
migration is pending — the boot whose backup mattered. Nothing in the log explains it, because the
only handler is around the SQLite copy that succeeded.

```python
def _rotate(backup_dir: Path, max_keep: int) -> None:
    """Keep only the most recent `max_keep` backups, delete the rest.

    Best-effort by design. This runs from `run_migrations`, which is the first statement of the
    app's lifespan — so a stale file that cannot be stat'd or unlinked (a vanished file racing the
    scheduled backup, a locked or read-only mount) used to crash-loop the container over
    housekeeping, on the one boot where a migration was pending.
    """
    try:
        backups = sorted(backup_dir.glob("shortlist_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        logger.warning("could not list backups to rotate ({}) — keeping them all", type(e).__name__)
        return
    for old in backups[max_keep:]:
        try:
            old.unlink()
        except OSError as e:
            logger.warning("could not rotate {} ({}) — leaving it", old.name, type(e).__name__)
            continue
        logger.debug("rotated old backup: {}", old.name)
```

Test (fails today — today it raises out of `take_backup`):

```python
def test_a_backup_that_cannot_be_rotated_does_not_sink_the_boot(self, tmp_path, monkeypatch):
    """`_rotate` is called OUTSIDE take_backup's try/except and has none of its own, and
    `run_migrations` — which calls it — is the first line of the app's lifespan. A locked stale file
    crash-looped the container over housekeeping."""
    monkeypatch.setattr(Path, "unlink", _raising(PermissionError))
    ...
    assert take_backup(tmp_path, max_keep=1) is not None  # the NEW backup still succeeded
```

`api/system.py`'s restore endpoint has the same shape around `shutil.copy2` (`backup.py:126`) but is
a manual admin action rather than a boot path, so it produces a bare 500 rather than a crash-loop.
Worth the same treatment; not worth blocking on.

**Which two are the audit's two?** `create_pin` and `poll_pin` are the confident pair: they are in
the same file as `owned_machine_ids`, whose docstring states the house rule verbatim, and they are
the only two `r.json()`-then-index sites in that file that do not follow it. Two independent
searches converged on them. The `_rotate` gap is real regardless and is cheap; whether it belongs in
this item or its own is in §2.6.

**Tests first** — all fail today (today they raise, or return 500).
`tests/integration/test_api_auth_setup.py` already uses `respx`, so these are cheap:

```python
class TestPlexTvFailuresDuringLoginAreExplained:
    """The login handshake is the one flow with no other way to report anything, and it was the one
    place that let a plex.tv failure out as an unhandled 500 — the exact symptom `owned_machine_ids`
    was hardened against, two functions above these."""

    def test_pin_creation_reports_an_unreachable_plex_tv_as_503(self, client):
        with respx.mock:
            respx.post("https://plex.tv/api/v2/pins").mock(side_effect=httpx.ConnectError("no route"))
            r = client.post("/api/auth/pin")
        assert r.status_code == 503 and "plex.tv" in r.json()["detail"]

    def test_pin_creation_reports_a_captive_portal_html_body_as_502(self, client):
        with respx.mock:
            respx.post("https://plex.tv/api/v2/pins").mock(
                return_value=httpx.Response(200, text="<html>Sign in to the hotel wifi</html>")
            )
            r = client.post("/api/auth/pin")
        assert r.status_code == 502

    def test_pin_creation_refuses_a_json_body_with_no_code(self, client):
        with respx.mock:
            respx.post("https://plex.tv/api/v2/pins").mock(return_value=httpx.Response(200, json={"id": 42}))
            r = client.post("/api/auth/pin")
        assert r.status_code == 502

    def test_polling_reports_an_unreachable_plex_tv_as_503(self, client):
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(side_effect=httpx.ReadTimeout("slow"))
            r = client.get("/api/auth/pin/42")
        assert r.status_code == 503

    def test_polling_refuses_an_account_body_with_no_id(self, client):
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(
                return_value=httpx.Response(200, json={"authToken": "pin-token-sentinel"})
            )
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json={"username": "steve"}))
            r = client.get("/api/auth/pin/42")
        assert r.status_code == 502

    def test_an_expired_pin_still_says_start_over(self, client):
        """The 404 branch is a real answer, not a failure. `HTTPException` is not an `httpx.HTTPError`,
        so it must pass through the new `except` untouched — this is the test that proves it."""
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(return_value=httpx.Response(404))
            r = client.get("/api/auth/pin/42")
        assert r.status_code == 404 and "start over" in r.json()["detail"]

    def test_an_unapproved_pin_is_still_linked_false_not_an_error(self, client):
        """The NORMAL case, polled every ~1.5s. Turning this into an error would break every login."""
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(return_value=httpx.Response(200, json={}))
            assert client.get("/api/auth/pin/42").json() == {"linked": False, "account_id": None, "username": None}
```

### 2.4 (d) Off-by-one bound — real, and smaller than the audit implies

`auth.py` has four windowed limiters. Three check the budget **before** recording the hit; one
records first:

```
auth.py:60   if len(_PIN_ALL) >= _PIN_MAX_GLOBAL:      # checked, appended at :74   -> 60 allowed
auth.py:67   if len(hits) >= _PIN_MAX_PER_WINDOW:      # checked, appended at :73   -> 10 allowed
auth.py:92   if len(_POLL_ALL) >= _POLL_MAX_GLOBAL:    # checked, appended at :94   -> 600 allowed
auth.py:263  if len(_TOKEN_FAILS) >= _TOKEN_MAX_FAILS: # APPENDED FIRST, at :286    -> 19 allowed
```

In `require_owner` (`auth.py:286-290`) the order is `_TOKEN_FAILS.append(...)` → `logger.warning` →
`_rate_limit_token_failures()` → `raise HTTPException(401)`. So the 20th bad token in a window
appends, pushes the length to 20, trips `>= 20`, and returns **429 instead of the 401** the next line
was about to raise. The effective budget is 19 clean 401s, not 20, and the boundary attempt gets a
different status code from its 19 predecessors.

Nothing security-relevant turns on it. It is worth fixing because four limiters in one file should
not disagree about what their constant means — and it is small enough that if this is _not_ what the
audit meant by "off-by-one bound", the item should be re-cited rather than assumed closed.

```python
    bearer = _bearer_token(request)
    if bearer is not None:
        if owner_id is not None and request.app.state.verify_api_token(bearer):
            return {"account_id": owner_id, "via": "api_token"}
        # Budget checked BEFORE the failure is recorded, matching the three PIN/poll limiters above.
        # Counting first made the Nth bad token answer 429 while the N-1 before it answered 401, so
        # `_TOKEN_MAX_FAILS = 20` actually allowed 19.
        _rate_limit_token_failures()
        _TOKEN_FAILS.append(time.monotonic())
        logger.warning("rejected an invalid or revoked API token")
        raise HTTPException(status_code=401, detail="invalid or revoked API token")
```

Test (fails today — today the 20th attempt is a 429), in `tests/unit/test_auth_rate_limit.py`, which
already manipulates these module-level deques and must reuse whatever it does to reset them between
cases (they are process-global for the whole session):

```python
def test_the_token_budget_allows_exactly_max_fails_before_throttling(self):
    """All four limiters in this file take the same shape: check the budget, then record the hit.
    This one recorded first, so the boundary attempt got 429 where its predecessors got 401."""
    auth._TOKEN_FAILS.clear()
    for _ in range(auth._TOKEN_MAX_FAILS):
        with pytest.raises(HTTPException) as caught:
            require_owner(_request("GET", owner=555, bearer="shl_bad", valid_token="shl_good"))
        assert caught.value.status_code == 401
    with pytest.raises(HTTPException) as caught:
        require_owner(_request("GET", owner=555, bearer="shl_bad", valid_token="shl_good"))
    assert caught.value.status_code == 429
```

**Bounds checked and found CORRECT**, recorded so nobody re-checks them: `backup._rotate`
(`services/backup.py:76` — `backups[max_keep:]` keeps exactly `max_keep`); the job retry ladder
(`services/jobs.py:595` — `job.attempts < job.max_attempts` with `attempts += 1` at claim time,
`jobs.py:556`, giving exactly `max_attempts` tries); the backoff index
(`jobs.py:548` — `_BACKOFF_S[min(job.attempts - 1, len(_BACKOFF_S) - 1)]`, correctly clamped);
`log_reader.tail_text` (`services/log_reader.py:79-82` — seeks, then discards the partial line).

**One bound that is not off-by-one but is a real behaviour worth the owner's eye.**
`api/notifications.py:74` caps the dismissed-id list at `[*current, body.id][-100:]`. Past 100
dismissals the oldest id is evicted and the notification it silenced **comes back**. Most dismissable
ids encode their state (a version, a run id) so they churn naturally — but `owner-sees-all-rows` has
a deliberately stable id _"so dismissing it means dismissing it for good"_
(`notifications.py:391`), and that promise holds only for the first 100 dismissals. Not part of this
item; flagged so it is not lost.

### 2.5 Settings / migration / API / UI impact

- No settings, no schema, no migration.
- **API**: `POST /api/auth/logout` gains a 403 for a missing CSRF header; `POST /api/auth/pin` and
  `GET /api/auth/pin/{id}` gain 502/503 in place of unhandled 500s. All three already declare
  `response_model`s with `extra="allow"`. Check `tests/unit/test_openapi_snapshot.py` before
  assuming the snapshot does not record status codes.
- **UI**: the SPA already sends the CSRF header on every mutation (`web/src/lib/api.ts:164-165`), so
  logout is unaffected. The login screen now receives a real `detail` string on a plex.tv failure —
  confirm `web/src/pages/login.tsx` surfaces it via `apiErrorMessage` rather than a status code
  (`rules/frontend.md`: "errors say what went wrong and how to fix it — never raw error codes").
- **Architecture Review: required.** This touches auth and tokens, which is on `.claude/CLAUDE.md`'s
  risk list. (b) also touches a rule-9 path.
- Verification while iterating: `pytest tests/unit/test_auth_gate.py tests/unit/test_auth_rate_limit.py
tests/unit/test_redaction.py tests/integration/test_api_auth_setup.py -q`. Full suite before the
  commit.

### 2.6 Open questions

- **Is `run_service.py:357-362` (§2.2, adjacent finding) part of this item?** It is the same rule and
  a two-line fix, but it widens the diff into the run path and the SSE payload.
- Should `_plextv_body`'s 502 distinguish "plex.tv said no" from "plex.tv said something we can't
  parse"? The copy above does, at the cost of two more strings; the design doc's voice rule argues
  for keeping them distinct.
- The dismissed-list cap (§2.4, last paragraph) contradicts a stated promise. Own item, or fold in?
- **(d) is small.** If the audit meant a different off-by-one, it needs a `file:line`; the bounds
  swept and cleared are listed above so the search is not repeated blind.
- **Three try/except candidates, not two.** `create_pin` + `poll_pin` are the confident pair;
  `backup._rotate` (§2.3) is a genuine boot-path crash-loop found by a separate sweep. All three are
  cheap; fixing all three is the obvious answer unless the item is being kept to exactly what the
  audit counted.
- `api/system.py`'s restore endpoint has `_rotate`'s shape around `shutil.copy2`. Same fix, lower
  stakes (a manual button, a 500 not a crash-loop). Fold in or leave?

---

## 3. `secret-key` — a lost key is silent, and then it destroys the ciphertext

### 3.1 What is actually wrong

The audit says: _"Lost key silently regenerates instead of failing with a clear diagnosis."_ **That
is true, and it understates the problem.** The silent regeneration is the trigger; the damage is done
sixty lines later, by a function whose comment says it is doing the opposite.

**The whole chain, in boot order.**

**(1) The key is regenerated with no log line at all** —
`shortlist/server/services/secrets.py:14-23`:

```python
    def __init__(self, config_dir: Path):
        key_path = config_dir / "secret.key"
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        self._fernet = Fernet(key_path.read_bytes())
```

There is no `logger` import in this module at all. Compare `main._instance_secret`
(`shortlist/server/main.py:59-79`), which handles the analogous case for `session.secret` and _does_
warn — _"{} was empty or truncated — generating a new one (existing sessions end)"_. The `SecretBox`
comment even names that function as the shape it copies; it copied the `os.open` mode trick and not
the warning.

**(2) The old ciphertext is then overwritten, and reported as a security improvement** —
`shortlist/server/settings_store.py:436-448`, called from `main.py:202` on every boot:

```python
        for key in sorted(SECRET_KEYS):
            row = self._session.get(Setting, key)
            value = (row.value or {}).get("v") if row else None
            if not value or not isinstance(value, str):
                continue
            try:
                self._secrets.decrypt(value)
            except Exception:  # any decrypt failure means the value is not encrypted
                row.value = {"v": self._secrets.encrypt(value)}
                healed.append(key)
```

`# any decrypt failure means the value is not encrypted` is false, and it is the whole bug.
**A Fernet decrypt failure has two causes and the exception cannot tell them apart** — measured
directly against this project's own `cryptography`:

```
wrong key -> cryptography.fernet.InvalidToken ''
plaintext -> cryptography.fernet.InvalidToken ''
```

Same type, same empty message. So on a boot with a regenerated key, every genuinely-encrypted secret
is classified as plaintext and **re-encrypted with the new key**, storing
`Fernet_new(Fernet_old(plaintext))`. Also measured: `f2.decrypt(f2.encrypt(t)) == t` — the double
wrapping round-trips silently, and each subsequent lost-key boot adds another layer.

**(3) The result is presented to the owner as a healthy install.** `main.py:202-205` logs
`"encrypted N setting(s) that were stored in the clear: curator.api_key, plex.token, …"` — a warning
describing a rule-9 _repair_, at the moment a rule-9 _disaster_ happened. `SettingsStore.get`
(`settings_store.py:359-360`) then peels one layer successfully and returns the **inner ciphertext
string** as if it were the Plex token, so Shortlist sends a base64 blob as `X-Plex-Token` and every
call 401s. Meanwhile `all_public()` (`settings_store.py:415`) redacts on truthiness alone, so the
Settings page shows `•••••` beside every credential — "your Plex token is set" — while none of them
work. The owner's symptom is "Plex stopped working"; the cause is a missing file that nothing
mentioned.

**(4) The one log line that exists never reaches the durable log.** `configure_logging` is not
called until `main.py:211` — **after** the warning at `:202-205`. Before that call there is no file
sink at all (`shortlist/logging_config.py:79-86` does `logger.remove()` then adds both sinks), so the
misleading warning goes to the console only and never to `/config/logs/shortlist.log`: the file the
support bundle exports and the owner reads a week later. **Any diagnosis added here must be emitted
after line 211**, or it is invisible tomorrow.

**(5) Backups do not cover it.** `services/backup.py:30-68` copies `shortlist.db` and nothing else,
so `/config/secret.key` is in no backup Shortlist takes, and restoring any backup on an instance with
a regenerated key reproduces the failure exactly. The pre-migration backup (`db/session.py:161-163`)
is taken **only when a migration is actually pending**, so a plain restart with a lost key leaves no
pre-corruption copy either.

### 3.2 What is recoverable, and what is not

| Secret                                                                                                                        | Recoverable without the old key? | How                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------- |
| `plex.token`                                                                                                                  | yes                              | re-link Plex (wizard), or paste a token in Settings (`api/settings.py:419`)               |
| `tmdb.apikey`, `curator.api_key`, `tautulli.apikey`, `requests.*.apikey`, `trakt.client_id`, `exa.apikey`, `searxng.password` | yes                              | owner re-enters each in Settings; `store.set` re-encrypts with the new key                |
| `api.token` (our own)                                                                                                         | **no**                           | must be regenerated (`POST /api/system/api-token`); every script using the old one breaks |
| `servers.token_enc`                                                                                                           | n/a                              | written at link time (`api/setup.py:290,300`) and **never read anywhere** — see §3.8      |

So the loss is recoverable **as long as the ciphertext survives**. Today it is overwritten on the
first boot after the key goes missing. Preserving it is the part of this fix that is not optional and
not an owner decision.

### 3.3 Common to both options — required either way

Three changes, none of which alter whether the app boots.

**(i) Stop treating "cannot decrypt" as "must be plaintext".** A Fernet token is
`base64url(0x80 ‖ timestamp[8] ‖ IV[16] ‖ ciphertext ‖ HMAC[32])`. Measured: the shortest real token
decodes to 73 raw bytes and starts with `0x80`; the spec floor is 57.

```python
_FERNET_VERSION = 0x80
_FERNET_MIN_BYTES = 57  # 1 version + 8 timestamp + 16 IV + 32 HMAC; a real token reaches >= 73


def looks_like_ciphertext(value: str) -> bool:
    """Whether `value` has the shape of a Fernet token we merely cannot READ.

    The one question `decrypt()` cannot answer. A wrong key and a plaintext value both raise a bare
    `InvalidToken` with an empty message — measured — so the failure alone cannot tell "this was
    never encrypted" from "this was encrypted with a key I no longer have", and those two demand
    OPPOSITE actions: encrypt it, or do not touch it.

    Deliberately generous. A false "this is ciphertext" leaves one plaintext secret unencrypted and
    loudly reported; a false "this is plaintext" destroys the only copy of a credential. So the
    length floor is the spec's 57 rather than the 73 a real token actually reaches.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except (ValueError, binascii.Error):
        return False
    return len(raw) >= _FERNET_MIN_BYTES and raw[0] == _FERNET_VERSION
```

This does **not** contradict the existing docstring's reasoning (_"Detection is by decryptability
rather than a prefix check, so it cannot be fooled by a key that merely looks Fernet-shaped"_). That
sentence guards against leaving a plaintext value unencrypted because it _looked_ encrypted — and
decryptability still decides that case first. The shape test runs only on values that already
**failed** to decrypt, to split the one bucket the old code could not split.

```python
    def heal_and_audit_secrets(self) -> tuple[list[str], list[str]]:
        """Encrypt any SECRET_KEY still in the clear; report any we can no longer read.

        Returns `(healed, unreadable)` — both lists of KEY NAMES, never values.

        The second list is the one that matters. `# any decrypt failure means the value is not
        encrypted` was wrong: a wrong key and a plaintext value raise the identical bare
        `InvalidToken`, so a boot that regenerated /config/secret.key classified every genuinely
        encrypted credential as plaintext and re-encrypted it with the new key — turning a
        RECOVERABLE loss (put the old key file back) into an unrecoverable one, and logging it as
        "encrypted N setting(s) that were stored in the clear".
        """
        if not self._secrets:
            return [], []
        healed: list[str] = []
        unreadable: list[str] = []
        for key in sorted(SECRET_KEYS):
            row = self._session.get(Setting, key)
            value = (row.value or {}).get("v") if row else None
            if not value or not isinstance(value, str):
                continue
            try:
                self._secrets.decrypt(value)
                continue  # already ours, nothing to do
            except InvalidToken:
                pass
            if looks_like_ciphertext(value):
                # Encrypted with a key we do not have. LEAVE IT EXACTLY AS IT IS: it is the only
                # copy, and restoring /config/secret.key brings it straight back.
                unreadable.append(key)
                continue
            row.value = {"v": self._secrets.encrypt(value)}
            healed.append(key)
        if healed:
            self._session.commit()
        return healed, unreadable
```

Keep `encrypt_plaintext_secrets` as a thin wrapper returning `healed` only, so no caller outside
`main.py` has to change.

**(ii) `get` must stop raising.** With the heal fixed, an unreadable row now survives into
`SettingsStore.get`, which calls `self._secrets.decrypt(value)` unguarded
(`settings_store.py:359-360`). Three live callers would 500 on it: `main.holds_secrets`
(`main.py:159-172`, reached by `GET /api/auth/session` on an unlinked instance), `_settings_diff`
(`api/settings.py:71-80`, which reads _every_ key on every Settings PUT), and
`context_builder.build_context` (`services/context_builder.py:375-376`).

```python
        if key in SECRET_KEYS and value:
            try:
                return self._secrets.decrypt(value)
            except InvalidToken:
                # Encrypted with a key this instance no longer has. Fall back rather than raise: this
                # is read at boot and on every Settings PUT, so raising is a crash loop plus a 500 on
                # the very page that would let the owner FIX it. The alarm is raised once, at boot,
                # by `heal_and_audit_secrets` — deliberately not here, because `get` runs thousands
                # of times a night and a per-call warning would bury the boot line.
                return DEFAULTS.get(key, default)
```

`plex.token` has no default, so this returns `None` and `context_builder` raises its existing
`RuntimeError("Plex connection is not configured yet — finish setup first")` — an honest, already
worded failure.

**(iii) The diagnosis must be logged where it survives**, i.e. below `configure_logging`:

```python
healed, unreadable = store.heal_and_audit_secrets()
store.seed_from_env(dict(os.environ))
(config_dir / "logs").mkdir(parents=True, exist_ok=True)
configure_logging(store.get("log.level"), log_file=str(config_dir / "logs" / "shortlist.log"))
# BELOW configure_logging on purpose: before this line there is no file sink, so anything
# logged above reaches `docker logs` and never /config/logs/shortlist.log — the file the
# support bundle exports and the one an owner reads after the fact.
if healed:
    logger.warning("encrypted {} setting(s) that were stored in the clear: {}", len(healed), ", ".join(healed))
if unreadable:
    app.state.unreadable_secrets = unreadable
    logger.error(
        "CANNOT READ {} saved credential(s): {}. /config/secret.key does not match what "
        "encrypted them — it was regenerated, replaced, or restored from a different "
        "instance. The stored values have NOT been altered, so putting the original "
        "secret.key back and restarting restores everything. If it is gone for good, "
        "re-enter each of these in Settings; the API token must be regenerated.",
        len(unreadable),
        ", ".join(unreadable),
    )
    session.add(
        Event(
            scope="secrets.unreadable", level="error", message={"keys": unreadable, "at": datetime.now(UTC).isoformat()}
        )
    )
    session.commit()
```

`app.state.unreadable_secrets` (default `[]`) is the flag every surface reads. **Key names only** —
never a value, never a key fingerprint.

### 3.4 Option A — refuse to boot

```python
if unreadable and not _accept_lost_key():  # SHORTLIST_ACCEPT_LOST_SECRET_KEY=1
    raise RuntimeError(
        f"refusing to start: {len(unreadable)} saved credential(s) cannot be decrypted "
        f"({', '.join(unreadable)}). Restore /config/secret.key, or set "
        "SHORTLIST_ACCEPT_LOST_SECRET_KEY=1 to start anyway and re-enter them."
    )
```

**Trade-off.** A refusal to boot is the only signal that cannot be scrolled past: nothing runs, no
scheduled job fires against a half-configured client, and the owner must decide before the instance
does anything at all. It matches how this codebase already treats ambiguity elsewhere — `_sweep_phase`
aborts the whole run on a raise, `owned_machine_ids` fails closed on an unreadable answer. The cost
is that the diagnosis becomes readable only in `docker logs`, and the UI — the one surface that could
list which credentials to re-enter _and_ provide the fields to do it — is precisely what a crash-loop
takes away. On a host that recreates containers on a timer (this deployment's watchtower polls every
4h), a crash-loop reads as "the new image is broken", which sends the owner to the wrong diagnosis
first; and an owner whose key is genuinely gone must find an environment variable, edit a compose
file and redeploy before they can even see the list of what to re-enter.

### 3.5 Option B — boot degraded, and say so everywhere

No `raise`. The instance starts with `app.state.unreadable_secrets` populated, and three surfaces
report it.

**1. A non-dismissable `error` notification**, added to `build_notifications`
(`shortlist/server/notifications.py:687-711`) beside `_runs_paused` — the existing precedent for
"a condition still true right now, where hiding the alert hides the thing itself"
(`notifications.py:7-9`):

```python
def _secrets_unreadable(state) -> dict | None:
    """Shortlist cannot decrypt its own saved credentials — /config/secret.key does not match.

    Non-dismissable, like `_runs_paused`: the condition is live, and every other surface says the
    opposite. `all_public()` redacts a secret on truthiness alone, so the Settings page shows
    "•••••" beside a Plex token that has not worked since the key changed.
    """
    keys = getattr(state, "unreadable_secrets", None)
    if not keys:
        return None
    return {
        "id": "secrets-unreadable",
        "severity": "error",
        "title": "Shortlist can't read its saved credentials",
        "body": (
            f"{len(keys)} saved credential(s) were encrypted with a key this instance no longer has "
            f"({', '.join(keys)}). Nothing has been overwritten — putting the original "
            "/config/secret.key back and restarting brings them all back. If it's gone, re-enter "
            "each one in Settings and regenerate the API token."
        ),
        "action_url": "/settings",
        "action_label": "Settings",
        "dismissable": False,
    }
```

`build_notifications(session, store, current_version)` has no `state` parameter today, so this needs
either a fourth argument or a read of the newest `secrets.unreadable` event. **Prefer the
parameter** — the event row is a historical record and would keep firing the alert after the key was
restored.

**2. Runs fail with the existing wording.** `context_builder` already raises "Plex connection is not
configured yet — finish setup first" when `plex.token` is falsy. No change; the notification supplies
the real reason.

**3. Settings shows it** — recommended, not required. See §3.7.

**Trade-off.** Booting degraded keeps the only surface that can explain the problem and the only
place the credentials can be re-entered, and it puts the diagnosis in the app, in `docker logs`, and
in the durable log file rather than in `docker logs` alone. Nothing runs against a corrupted
credential, because reads return the default and the run refuses itself with an error that already
exists. The cost is that a soft failure can be lived with: an owner who mentally filters the banner
has an instance that quietly stops curating, and "runs are doing nothing" is a state this product can
reach for half a dozen other reasons — a less distinctive signal than a dead container. The
mitigation is that the alert is `error`, non-dismissable, and names the exact keys.

### 3.6 Recommendation

**Option B.**

The irreversible damage in this bug is not that the app boots — it is `encrypt_plaintext_secrets`
overwriting the only copy of every credential and logging it as a repair. That fix is common to both
options and is where the entire loss comes from. Once the ciphertext is preserved, "the app started
and told you which credentials it cannot read" is strictly more useful than "the app did not start",
because the recovery for a genuinely-lost key **is** re-entering credentials in Settings — and
Option A's escape hatch is a compose-file edit standing between the owner and that screen. On a
headless, auto-recreating deployment a crash-loop is the worst available diagnosis channel: it looks
like a bad image, and it removes the UI, the log page and the support bundle at the same time.

Option A is right if — and only if — the owner's position is _"an instance that cannot read its own
credentials must never run at all, even degraded"_. That is a legitimate position and it is the
owner's to take. If it is taken, keep every part of §3.3: hard-failing **without** preserving the
ciphertext would be the worst of both.

**Not recommended either way: putting `secret.key` into the backups.** It would sit beside the
ciphertext it protects and defeat encryption at rest. Instead `docs/guides.md` and the Backups page
should say plainly that a backup of `/config` must include `secret.key`, and that a database backup
alone cannot restore credentials.

### 3.7 Tests, written first

**`tests/unit/test_settings_store.py`** — a new class beside `TestASecretNeedsASecretBox`, reusing
its `sessions` fixture:

```python
class TestALostSecretKeyIsNotMistakenForPlaintext:
    """`# any decrypt failure means the value is not encrypted` was wrong. A wrong key and a
    plaintext value raise the identical bare `InvalidToken`, so a boot that regenerated
    /config/secret.key re-encrypted every real credential with the new key — destroying the only
    copy, and reporting it as "encrypted N setting(s) that were stored in the clear"."""

    def _two_instances(self, tmp_path: Path) -> tuple[SecretBox, SecretBox]:
        return SecretBox(tmp_path / "a"), SecretBox(tmp_path / "b")  # two unrelated keys

    def test_a_ciphertext_from_another_key_is_left_byte_identical(self, tmp_path, sessions):
        """FAILS TODAY: today the row is re-encrypted with the new key and the original ciphertext —
        the only thing restoring secret.key could recover — is gone."""
        old, new = self._two_instances(tmp_path)
        with sessions() as session:
            SettingsStore(session, old).set("plex.token", "a-token-sentinel")
            before = session.get(Setting, "plex.token").value["v"]

            healed, unreadable = SettingsStore(session, new).heal_and_audit_secrets()

            assert healed == [] and unreadable == ["plex.token"]
            assert session.get(Setting, "plex.token").value["v"] == before

    def test_the_original_key_still_decrypts_it_afterwards(self, tmp_path, sessions):
        """The point of leaving it alone: the loss stays RECOVERABLE. FAILS TODAY — after today's
        heal the old key cannot read the row at all, because it is now double-wrapped."""
        old, new = self._two_instances(tmp_path)
        with sessions() as session:
            SettingsStore(session, old).set("plex.token", "a-token-sentinel")
            SettingsStore(session, new).heal_and_audit_secrets()

            assert SettingsStore(session, old).get("plex.token") == "a-token-sentinel"

    def test_a_genuine_plaintext_secret_is_still_healed(self, tmp_path, sessions):
        """PASSES TODAY. `tmdb.apikey` was plaintext at rest on every install predating its move into
        SECRET_KEYS; the shape check must not break that path."""
        box = SecretBox(tmp_path)
        with sessions() as session:
            session.add(Setting(key="tmdb.apikey", value={"v": "abcdef0123456789"}))
            session.commit()

            healed, unreadable = SettingsStore(session, box).heal_and_audit_secrets()

            assert healed == ["tmdb.apikey"] and unreadable == []
            assert SettingsStore(session, box).get("tmdb.apikey") == "abcdef0123456789"

    def test_get_falls_back_instead_of_raising_when_a_secret_cannot_be_decrypted(self, tmp_path, sessions):
        """FAILS TODAY (`InvalidToken` escapes). `holds_secrets`, `_settings_diff` and
        `build_context` all read every secret, so a raise here is a 500 on `/api/auth/session` and on
        the Settings PUT — the page the owner needs in order to fix it."""
        old, new = self._two_instances(tmp_path)
        with sessions() as session:
            SettingsStore(session, old).set("plex.token", "a-token-sentinel")

            assert SettingsStore(session, new).get("plex.token") is None

    @pytest.mark.parametrize("key", sorted(SECRET_KEYS))
    def test_every_secret_key_is_covered_not_just_plex_token(self, key, tmp_path, sessions):
        """Same reasoning as its sibling above: the guard has to be the set, not a hand-picked
        subset that drifts."""
        old, new = self._two_instances(tmp_path)
        with sessions() as session:
            SettingsStore(session, old).set(key, "a-value-sentinel")
            _healed, unreadable = SettingsStore(session, new).heal_and_audit_secrets()
        assert unreadable == [key]


class TestLooksLikeCiphertext:
    """The one discriminator, tested directly — `decrypt()` cannot answer this question."""

    def test_a_real_fernet_token_of_any_length(self, tmp_path):
        box = SecretBox(tmp_path)
        for plaintext in ("", "x", "a" * 200, "a" * 4000):
            assert looks_like_ciphertext(box.encrypt(plaintext))

    @pytest.mark.parametrize(
        "value", ["", "shl_abc", "sk-ant-api03-xxxx", "1234567890abcdef1234", "not base64 at all!!", "AAAA"]
    )
    def test_a_credential_shaped_plaintext_is_not(self, value):
        assert not looks_like_ciphertext(value)
```

**`tests/unit/test_server_core.py::TestSecretBox`** — the regeneration itself:

```python
    def test_it_reports_whether_it_created_the_key_or_loaded_one(self, tmp_path):
        """FAILS TODAY: `SecretBox` has no such attribute and no logger import. A missing key is the
        most destructive thing that can happen to this instance's data, and it happened with no
        output at all — `main._instance_secret` warns for the far less severe session-secret case."""
        assert SecretBox(tmp_path).created is True
        assert SecretBox(tmp_path).created is False
```

**Boot-level, `tests/unit/test_server_core.py`** — the shape that actually pins the bug:

```python
class TestBootWithALostSecretKey:
    def test_a_regenerated_key_does_not_overwrite_the_stored_credentials(self, tmp_path):
        """FAILS TODAY. The whole bug in one test: boot, save a token, delete secret.key, boot again,
        put the key back, boot a third time — and the token must still be there."""
        with TestClient(create_app(config_dir=tmp_path)) as app_a:
            with app_a.app.state.sessions() as s:
                SettingsStore(s, app_a.app.state.secrets).set("plex.token", "a-token-sentinel")
        saved = (tmp_path / "secret.key").read_bytes()

        (tmp_path / "secret.key").unlink()
        with TestClient(create_app(config_dir=tmp_path)) as app_b:
            assert app_b.app.state.unreadable_secrets == ["plex.token"]

        (tmp_path / "secret.key").write_bytes(saved)
        with TestClient(create_app(config_dir=tmp_path)) as app_c:
            assert app_c.app.state.unreadable_secrets == []
            with app_c.app.state.sessions() as s:
                assert SettingsStore(s, app_c.app.state.secrets).get("plex.token") == "a-token-sentinel"

    def test_the_diagnosis_reaches_the_log_file_not_just_the_console(self, tmp_path):
        """The misleading warning is emitted at main.py:202, BEFORE configure_logging adds the file
        sink at :211 — so today it never reaches /config/logs/shortlist.log, the file the support
        bundle exports. FAILS TODAY."""
        ...  # boot with a lost key
        log = (tmp_path / "logs" / "shortlist.log").read_text()
        assert "plex.token" in log  # the KEY NAME is named
        assert "a-token-sentinel" not in log  # the VALUE never is (rule 9)

    def test_a_genuinely_first_boot_is_silent(self, tmp_path):
        """No key and no secrets is a normal first run. It must not warn, must not alert, and must
        not refuse anything."""
        with TestClient(create_app(config_dir=tmp_path)) as app:
            assert app.app.state.unreadable_secrets == []
```

**`tests/unit/test_notifications.py`:**

```python
def test_a_lost_secret_key_raises_a_non_dismissable_error(...):
    """Non-dismissable, like "runs are paused": the condition is live and every other surface says
    the opposite — `all_public()` shows "•••••" beside a token that has not worked since the key
    changed. Asserts the body names the KEYS and never a value."""
```

**Prove the teeth.** This is exactly the "risky or subtle" logic the project asks for a positive
control on. Break `looks_like_ciphertext` to `return False`; both
`test_a_ciphertext_from_another_key_is_left_byte_identical` and
`test_the_original_key_still_decrypts_it_afterwards` must fail. Per `.claude/CLAUDE.md`: copy the
file to a backup first, never `git checkout` it back.

### 3.8 What could regress, and which test catches it

| Regression                                                                                         | Caught by                                                                                                                             |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| A genuinely plaintext secret is now left in the clear (rule 9) because the shape test over-matched | `test_a_genuine_plaintext_secret_is_still_healed` + `TestLooksLikeCiphertext`                                                         |
| `get` swallowing `InvalidToken` also swallows the `RuntimeError` for a box-less store              | existing `test_reading_a_secret_without_a_box_raises_instead_of_handing_back_ciphertext` (`_require_box` runs first and is untouched) |
| A secret read returns `None` and a caller silently treats it as "not configured"                   | `context_builder` raises its existing RuntimeError; the notification is the real cover                                                |
| The heal's return type changes and `main.py:202`'s walrus breaks                                   | type checker + `test_a_genuine_plaintext_secret_is_still_healed`; keep `encrypt_plaintext_secrets` as a wrapper                       |
| The alert stays up after the key is restored                                                       | third boot in `test_a_regenerated_key_does_not_overwrite_the_stored_credentials` asserts `unreadable_secrets == []`                   |
| A secret VALUE reaches the log or the notification body                                            | `test_the_diagnosis_reaches_the_log_file_not_just_the_console`; only key names are ever passed                                        |

### 3.9 Settings / migration / API / UI impact

- **No migration.** Nothing about the schema changes; the stored values are deliberately left
  byte-identical.
- **Settings**: no new key. `app.state.unreadable_secrets` is process state, not a settings row — on
  purpose: a row would persist after the key was restored and keep alarming.
- **API**: `GET /api/notifications` gains one possible entry. `build_notifications` gains a
  parameter (§3.5) — check `api/notifications.py` and any test that calls it directly.
- **UI**: the bell renders the new notification with no change. **Recommended addition:** the
  Settings page currently shows `•••••` beside an unreadable credential, which is the same
  false-reassurance shape as item 1. Making `all_public()` return a distinct sentinel is a bigger
  change than it looks — the PUT path skips exactly `REDACTED_PLACEHOLDER` in **four** places
  (`api/settings.py:74`, `:108`, `:513`, `:887`), and a new sentinel would otherwise be written
  verbatim by any save of an untouched form. If it is done, all four must learn about it in the same
  commit.
- **Docs** (`.claude/rules/docs.md` requires them in the same PR): `docs/guides.md`'s backup/restore
  section must state that `/config/secret.key` is part of a backup and that a `.db` backup alone
  cannot restore credentials. `docs/reference.md` gains `SHORTLIST_ACCEPT_LOST_SECRET_KEY` only if
  Option A is chosen.
- **Architecture Review: required.** Squarely on `.claude/CLAUDE.md`'s risk list — it touches secrets
  and tokens, and changes a boot path that writes to the settings table.
- Verification, all local and fake-backed: `pytest tests/unit/test_settings_store.py
tests/unit/test_server_core.py tests/unit/test_notifications.py -q`, then the full suite. Nothing
  here needs a real server. **Do not rehearse this against SFLIX** — the failure mode under test is
  "the credentials cannot be read", and the recovery involves re-entering the owner's live Plex
  token.

### 3.10 Open questions

1. **Option A or B?** §3.6 recommends B. Owner's call; §3.3 lands either way.
2. **`servers.token_enc` is written and never read.** `api/setup.py:290,300` encrypt the Plex token
   into it; nothing anywhere reads the column back (the live token is `settings["plex.token"]`). It
   is a second encrypted copy of the most sensitive value on the instance — undecryptable after a key
   loss and useless before one. Drop it in a migration, or start reading it as a fallback; not both,
   and not in this PR.
3. **Should the heal also fingerprint the key?** A truncated hash of `secret.key` in a non-secret
   settings row would let boot say "the key CHANGED" even on an install with no secrets stored yet.
   It adds a durable artifact derived from the key, which cuts against rule 9's spirit even
   truncated. Deliberately not designed here; raised so the decision is recorded rather than skipped.
4. **Does `tests/unit/test_openapi_snapshot.py` pin notification payloads?** If so, the new entry
   needs a regenerated snapshot.

---

## Files read for this design (read-only, no edits)

`web/src/pages/watching-account.tsx` (1-300), `web/src/components/query-boundary.tsx`,
`web/src/lib/queries.ts` (95-118, 360-385), `web/src/lib/api.ts` (160-210),
`web/src/test/watching-account.test.tsx`, `tests/e2e/test_watching_account_e2e.py`;
`shortlist/server/auth.py` (whole), `shortlist/server/main.py` (55-215),
`shortlist/server/settings_store.py` (246-470), `shortlist/server/services/secrets.py`,
`shortlist/server/services/backup.py` (1-120), `shortlist/server/services/run_service.py` (345-370),
`shortlist/server/db/session.py` (140-175), `shortlist/server/notifications.py` (1-80, 380-440,
683-711), `shortlist/server/api/settings.py` (40-120, 440-520, 850-890),
`shortlist/server/api/setup.py` (85-150, 275-305), `shortlist/server/api/collections.py` (230-400,
1150-1200), `shortlist/server/api/support.py` (200-310, 470-560, 1950-2030),
`shortlist/server/api/requests.py` (300-385), `shortlist/server/api/system.py` (85-130, 190-220),
`shortlist/server/api/notifications.py` (60-80), `shortlist/server/services/jobs.py` (380-620),
`shortlist/server/services/log_reader.py` (60-200),
`shortlist/server/services/context_builder.py` (360-400),
`shortlist/server/api/watching_account.py` (structure only), `shortlist/logging_config.py` (70-86),
`shortlist/engine/pipeline.py` (1555-1580), `shortlist/engine/clients/http_retry.py` (20-200),
`shortlist/engine/clients/tmdb.py` (35-100), `shortlist/engine/clients/arr.py` +
`clients/seerr.py` (auth transport only); `tests/conftest.py`, `tests/integration/conftest.py`,
`tests/integration/test_api_auth_setup.py`, `tests/unit/test_settings_store.py`,
`tests/unit/test_server_core.py`, `tests/unit/test_redaction.py`, `tests/unit/test_auth_gate.py`.

Plus two AST sweeps over `shortlist/server/` and `shortlist/engine/` (every `logger.*` call's
arguments, and every slice/limit bound), and a direct measurement of Fernet token shape and error
behaviour against this project's own `cryptography` build.
