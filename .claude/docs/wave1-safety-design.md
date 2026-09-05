# Wave 1 safety design — `oneclick-delete`, `wizard-dataloss`, `dry-run-gap`

From the September 2026 audit programme — see `.claude/docs/audit-2026-09-programme.md`, Wave 1
("Safety — can lose data"). The fourth Wave 1 item, `orphan-guard`, has its own document
(`.claude/docs/orphan-guard-design.md`) and is not repeated here.

Everything below was verified by reading the code. Where the audit's one-line premise turned out to
be wrong or overstated, the correction is stated first and the design is built on what is actually
true. Three of the three premises needed correcting to some degree.

---

# Item 1 — `oneclick-delete`

> Audit line: _"`jobs.tsx:747` — deletes Plex collections with no confirm."_

## 1. What is actually wrong

### 1a. The premise is half wrong: there IS a preview gate, and it is a good one

The call site is `web/src/pages/jobs.tsx:747-758`:

```tsx
{
  drifted.length + orphans.length > 0 && (
    <div>
      <Button
        size="sm"
        loading={driftFix.isPending}
        onClick={() => driftFix.mutate()}
      >
        Fix {drifted.length + orphans.length} row
        {drifted.length + orphans.length === 1 ? "" : "s"}
      </Button>
    </div>
  );
}
```

`driftFix` (`jobs.tsx:349-356`) posts `api.runJob("sync.check", { confirmed: true })`. `confirmed`
is what authorises the delete half of `_sync_check`
(`shortlist/server/services/jobs.py:880-936`, gate at line 897).

The audit's "no confirm" reads as though a stray click destroys collections out of nowhere. It does
not, and the existing protections are real and deliberate:

- **The button cannot exist until a dry run has come back.** `drifted`/`orphans`
  (`jobs.tsx:417-422`) are `[]` unless `driftPreview.data?.status === "done"`, and `driftPreview`
  (`jobs.tsx:345-348`) posts `{ dry_run: true }`. No preview, no button.
- **The count includes the deletions**, on purpose, so the label cannot understate the damage
  (pinned by `web/src/test/jobs-page.test.tsx:380-383`).
- **The deletions get a dedicated destructive callout above the button**
  (`jobs.tsx:713-727`) naming every collection, saying "This cannot be undone" — pinned by
  `jobs-page.test.tsx:356-384`.
- The server side is split correctly: a scheduled `sync.check` omits `confirmed` and therefore
  demotes but never deletes (`jobs.py:894-898`).

So this is not an unguarded destructive button. It is a **read-then-act** flow whose "read" step is
already well designed.

### 1b. What IS genuinely wrong

Three things, in descending order of how much they matter.

**(i) One click still separates reading the warning from destroying the collection, and nothing
re-states the consequence at the moment of the act.** The callout at `jobs.tsx:715` sits _above_ the
button and is easy to have scrolled past, dismissed as "the usual red box", or read minutes earlier —
`driftPreview.data` is a mutation result that persists until `driftFix` succeeds
(`jobs.tsx:354`) or the page unmounts. Every other irreversible Plex write in this SPA re-states its
consequence at the click:

| Action                                | Confirm                      | File                                                             |
| ------------------------------------- | ---------------------------- | ---------------------------------------------------------------- |
| Delete a row (+ its Plex collections) | `Dialog`                     | `components/rows/row-destructive-actions.tsx:80-120`             |
| Remove a row from Plex                | `Dialog`, gated on a dry run | `row-destructive-actions.tsx:122-186`                            |
| Turn a row off                        | `Dialog`                     | `components/rows/row-enable-toggle.tsx:36,60+`                   |
| Restore a backup                      | two-tap inline               | `components/jobs/backup-panel.tsx:40,156-190`                    |
| Remove a connection                   | two-tap inline               | `components/connection-card.tsx:126-128,274`                     |
| Enable/disable everyone               | `Dialog`                     | `pages/users.tsx` (pinned by `test/users-page.test.tsx:123,143`) |

"Delete N Plex collections for ever" is the single most destructive button on the Jobs page and the
only one of that class in the SPA with no confirm. That is the actual finding: **an inconsistency
with the codebase's own established rule**, not an unguarded button.

**(ii) The button bundles the reversible with the irreversible under one verb.** "Fix 3 rows" can
mean "demote three rows from Home" (monotonically private, undone by the next run — `jobs.tsx:344`
says so in a comment) or "demote two and permanently delete one". One label, one click, two very
different consequences.

**(iii) The preview can be stale, and the fix pass recomputes.** `driftFix` re-runs `sync.check`
server-side; it does not replay the preview's decisions. Between the two presses a run can finish
and change what is orphaned. The names in the callout are therefore _what the last check saw_, not a
contract. Nothing on screen says so.

## 2. The fix

**Design decision: use the existing `Dialog`, NOT shadcn `AlertDialog`.** The task brief assumed
`AlertDialog` is available. It is not — `web/package.json` has `@radix-ui/react-dialog` but no
`@radix-ui/react-alert-dialog`, there is no `web/src/components/ui/alert-dialog.tsx`, and
`web/node_modules/@radix-ui/` confirms the package is not installed. Adding it means a
`package.json` + `pnpm-lock.yaml` change, and the programme's own build-loop note records that
`pnpm` is not on PATH on this machine (`audit-2026-09-programme.md:276-282`). Every existing
destructive confirm in this SPA is built on `Dialog`, so a new dependency would buy a focus-trap
role attribute the codebase has already decided it does not need, at the cost of the one thing that
makes these dialogs reviewable — that they are all the same. (`window.confirm` is out under the
system rules and was never a candidate.)

**Design decision: confirm only when a deletion is involved.** When `orphans.length === 0` the fix
is demote-only: removal of visibility, reversible by the next run, and `jobs.tsx:344` already states
this is "never unsafe". A confirmation there is friction with no safety value, and
`row-enable-toggle.tsx:18-26` makes the codebase's position explicit — confirmations are for
consequences that are invisible, deferred, or cannot be undone. This also keeps the existing
demote-only test (`jobs-page.test.tsx:410`) passing unchanged, which is a feature: the diff is
provably scoped to the deleting case.

### 2a. `web/src/pages/jobs.tsx` — new state beside the mutations (after line 356)

```tsx
// Deleting a collection is the one irreversible thing this page does, and the callout that warns
// about it sits above the button — easy to have scrolled past, or read minutes ago. Every other
// irreversible Plex write in this app re-states its consequence at the click (row delete, row
// removal, disable-everyone); this was the exception. Only the DELETING case opens it: a
// demote-only fix removes visibility and the next run puts it back, so a confirm there is friction
// with no safety value.
const [confirmFix, setConfirmFix] = useState(false);
```

`useState` is already imported (`jobs.tsx:13`).

### 2b. Replace `jobs.tsx:747-758` with

```tsx
{
  drifted.length + orphans.length > 0 && (
    <div>
      <Button
        size="sm"
        variant={orphans.length > 0 ? "destructive" : "default"}
        loading={driftFix.isPending}
        onClick={() =>
          orphans.length > 0 ? setConfirmFix(true) : driftFix.mutate()
        }
      >
        Fix {drifted.length + orphans.length} row
        {drifted.length + orphans.length === 1 ? "" : "s"}
      </Button>
    </div>
  );
}
```

### 2c. The dialog, rendered inside the same `<div className="flex flex-col gap-3">` (after the

`driftFix.data` paragraph at `jobs.tsx:759-763`)

```tsx
<Dialog open={confirmFix} onOpenChange={setConfirmFix}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>
        Delete {orphans.length} collection
        {orphans.length === 1 ? "" : "s"} from Plex?
      </DialogTitle>
      <DialogDescription>
        Shortlist no longer knows who these belong to, so it can&rsquo;t hide
        them from anyone — leaving them means they sit in your Collections tab
        for ever, visible to everybody. Deleting them cannot be undone.
      </DialogDescription>
    </DialogHeader>
    {/* The names, again, at the moment of the act. The callout that carries them lives above the
        button and can have been read minutes ago or scrolled past entirely. */}
    <ul className="max-h-40 space-y-1 overflow-y-auto text-sm">
      {orphans.map((title) => (
        <li key={title} className="font-mono text-xs">
          {title}
        </li>
      ))}
    </ul>
    {drifted.length > 0 && (
      // The button says "Fix N rows" and N bundles both halves. Naming the reversible half here is
      // what stops the dialog reading as though every one of those N rows is about to be destroyed.
      <p className="text-sm text-muted-foreground">
        {drifted.length} other row{drifted.length === 1 ? " is" : "s are"} only
        being taken off your Home screen — that one comes back on the next run.
      </p>
    )}
    {/* The fix re-runs the check on the server; it does not replay this preview. Saying so is the
        difference between a list and a promise. */}
    <p className="text-sm text-muted-foreground">
      Shortlist re-checks Plex when you confirm, so what it finds may differ
      from this list.
    </p>
    {/* Inside the dialog, not beside the button that opened it: a failed fix leaves this dialog
        open, and everything behind an open dialog is aria-hidden — an alert out there would be
        invisible to a screen reader and buried under the overlay for everyone else. Copied
        deliberately from row-destructive-actions.tsx:88-97, which learned it the hard way. */}
    {driftFix.isError && (
      <p role="alert" className="text-sm text-destructive-text">
        {apiErrorMessage(driftFix.error, "Couldn’t fix those rows. Try again.")}
      </p>
    )}
    <DialogFooter>
      <Button variant="outline" onClick={() => setConfirmFix(false)}>
        Cancel
      </Button>
      <Button
        variant="destructive"
        loading={driftFix.isPending}
        onClick={() =>
          driftFix.mutate(undefined, { onSuccess: () => setConfirmFix(false) })
        }
      >
        Delete and fix
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

New imports at the top of `jobs.tsx`:

```tsx
import { apiErrorMessage } from "@/lib/api"; // extend the existing `import { api } from "@/lib/api"`
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
```

Note `driftFix.mutate(undefined, { onSuccess: ... })` — the per-call `onSuccess` runs _in addition
to_ the mutation's own `onSuccess: () => driftPreview.reset()` (`jobs.tsx:354`), which is what
already clears the stale preview. Closing on success rather than on click is deliberate: a failed
fix must leave the dialog open carrying its error, exactly as `row-destructive-actions.tsx:108-118`
does.

### 2d. What is deliberately NOT changed

- The callout at `jobs.tsx:713-727` stays. It is what makes the operator press the button in an
  informed state; the dialog is the second reading, not a replacement for the first.
- The `confirmed` payload contract (`jobs.tsx:353`) is untouched. This is a UI-only change; no
  endpoint, no job handler, no schema moves.

## 3. Tests, written first

New file is not needed — `web/src/test/jobs-page.test.tsx` already has the harness and the
`orphans: [...]` fixtures. Add to the same `describe` as `jobs-page.test.tsx:356`.

**Fails against today's code** (today there is no dialog, so the first click deletes):

```tsx
it("does not delete until the deletion is confirmed in a dialog", async () => {
  // The one irreversible thing this page does. Every other irreversible Plex write in this app
  // re-states its consequence at the click; this was the exception.
  runJob.mockResolvedValue({
    id: 1,
    kind: "sync.check",
    status: "done",
    detail: "",
    error: null,
    fixed: [],
    orphans: ["Shortlist_ghost"],
  });
  renderPage();
  await userEvent.click(
    await screen.findByRole("button", {
      name: /^Check now: Check and fix rows on Plex$/,
    }),
  );
  runJob.mockClear();

  await userEvent.click(
    await screen.findByRole("button", { name: /^Fix 1 row$/ }),
  );

  // Opening the confirmation must not itself delete anything — the same assertion
  // row-destructive-actions.test.tsx:123 makes about the row cleanup dialog.
  expect(runJob).not.toHaveBeenCalled();
  expect(await screen.findByRole("dialog")).toHaveTextContent(
    /cannot be undone/i,
  );
  // The names again, at the moment of the act, not only in the callout above the button.
  expect(screen.getByRole("dialog")).toHaveTextContent("Shortlist_ghost");
});
```

**Fails against today's code** (no "Delete and fix" button exists):

```tsx
it("sends `confirmed` only after the dialog is confirmed", async () => {
  runJob.mockResolvedValue({
    id: 1,
    kind: "sync.check",
    status: "done",
    detail: "Removed 1 orphaned collection(s)",
    error: null,
    fixed: [],
    orphans: ["Shortlist_ghost"],
  });
  renderPage();
  await userEvent.click(
    await screen.findByRole("button", {
      name: /^Check now: Check and fix rows on Plex$/,
    }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Fix 1 row$/ }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Delete and fix$/ }),
  );

  // Asserting the kwargs, not the call count: `confirmed` is the entire authorisation for the
  // delete half, and a call with the wrong payload is a preview reported as a deletion.
  expect(runJob).toHaveBeenLastCalledWith("sync.check", { confirmed: true });
});
```

**Fails against today's code** (today Cancel is impossible — the click already deleted):

```tsx
it("deletes nothing when the confirmation is dismissed", async () => {
  runJob.mockResolvedValue({
    id: 1,
    kind: "sync.check",
    status: "done",
    detail: "",
    error: null,
    fixed: [],
    orphans: ["Shortlist_ghost"],
  });
  renderPage();
  await userEvent.click(
    await screen.findByRole("button", {
      name: /^Check now: Check and fix rows on Plex$/,
    }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Fix 1 row$/ }),
  );
  runJob.mockClear();
  await userEvent.click(
    await screen.findByRole("button", { name: /^Cancel$/ }),
  );

  expect(runJob).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});
```

**PASSES against today's code, and must keep passing** — this is the one that pins the "confirm only
where it buys safety" decision, and it is why the existing `jobs-page.test.tsx:410` test
(`fixed: ["Shortlist_gemnath"]`, no orphans) needs no edit:

```tsx
it("fixes a demote-only drift with one click — nothing there is irreversible", async () => {
  // Converge only ever REMOVES visibility and the next run puts the row back, so a confirmation
  // here would be friction with no safety value (row-enable-toggle.tsx:18-26 states the rule).
  runJob.mockResolvedValue({
    id: 1,
    kind: "sync.check",
    status: "done",
    detail: "",
    error: null,
    fixed: ["Shortlist_gemnath"],
    orphans: [],
  });
  renderPage();
  await userEvent.click(
    await screen.findByRole("button", {
      name: /^Check now: Check and fix rows on Plex$/,
    }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Fix 1 row$/ }),
  );

  expect(runJob).toHaveBeenLastCalledWith("sync.check", { confirmed: true });
  expect(screen.queryByRole("dialog")).toBeNull();
});
```

**Fails against today's code** (the dialog does not exist, so there is nowhere for the error to be):

```tsx
it("shows a failed fix inside the dialog, where it is not aria-hidden", async () => {
  // Everything behind an open Radix dialog is aria-hidden. An error rendered outside it is
  // invisible to a screen reader and under the overlay for everyone else — the failure looks like
  // a button that did nothing. row-destructive-actions.tsx:88-97 records the same lesson.
  runJob
    .mockResolvedValueOnce({
      id: 1,
      kind: "sync.check",
      status: "done",
      detail: "",
      error: null,
      fixed: [],
      orphans: ["Shortlist_ghost"],
    })
    .mockRejectedValueOnce(new Error("Plex unreachable"));
  renderPage();
  await userEvent.click(
    await screen.findByRole("button", {
      name: /^Check now: Check and fix rows on Plex$/,
    }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Fix 1 row$/ }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: /^Delete and fix$/ }),
  );

  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByRole("alert")).toBeInTheDocument();
  expect(dialog).toBeInTheDocument(); // still open — a failure must not look like a success
});
```

(`within` is a new import from `@testing-library/react` in that file.)

## 4. What could regress, and which test catches it

| Regression                                                                                     | Caught by                                                                                                                                                       |
| ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dialog opens but the confirm button still sends no `confirmed` (or sends `dry_run`)            | `sends \`confirmed\` only after the dialog is confirmed` — asserts the kwargs, not the count                                                                    |
| Opening the dialog fires the mutation as a side effect                                         | `does not delete until the deletion is confirmed in a dialog` (`runJob` not called)                                                                             |
| The confirm gate is accidentally applied to demote-only fixes, adding a click to a safe action | `fixes a demote-only drift with one click`, plus the untouched `jobs-page.test.tsx:410`                                                                         |
| A failed fix silently closes the dialog and looks like success                                 | `shows a failed fix inside the dialog…`                                                                                                                         |
| The callout above the button is deleted "because the dialog says it now"                       | `jobs-page.test.tsx:356` (`warns loudly and separately when a preview would DELETE something`) still asserts the callout text                                   |
| The preview is not reset after a successful fix, leaving a stale "delete 1 collection" callout | existing `driftPreview.reset()` on `jobs.tsx:354`; add `expect(screen.queryByText(/this will delete/i)).toBeNull()` to the confirm test if the reset ever moves |

## 5. Settings / migration / API / UI impact

- **Settings:** none. **Migration:** none. **API:** none — no endpoint, payload or response changes.
- **OpenAPI snapshot:** untouched, so `tests/unit/test_openapi_snapshot.py` is unaffected.
- **Dependencies:** none added — this is the whole point of using `Dialog` over `AlertDialog`. No
  `pnpm-lock.yaml` change, no install needed.
- **UI:** one new dialog on the Jobs page; the "Fix N rows" button turns `destructive` when the fix
  includes a deletion. Accessibility comes from Radix `Dialog` (focus trap, `role="dialog"`, Esc to
  close), which is what every other confirm in the app already uses.
- **e2e:** no `tests/e2e/` spec drives this flow today (`grep -rn "sync.check" tests/e2e/` → nothing),
  so `-m e2e` neither covers it nor breaks. Adding one is optional and out of scope.
- **Docs:** `docs/guides.md`'s Jobs section should gain one sentence if it describes the Fix button
  step by step (`.claude/rules/docs.md`). Worth a grep at implementation time.

## 6. Open questions

1. **Should the dialog re-run the dry run on open, so the names it shows are current?** It would
   close finding (iii) properly instead of papering it with a sentence. Against: a second full Plex
   walk per confirmation, and the check can be queued behind a run (`jobs.tsx:730-737`), which would
   mean a dialog that opens onto a spinner. Not designed here — flagged, not guessed.
2. **Should there be a "Fix without deleting" escape** — demote everything, leave the orphans? The
   server supports it exactly (omit `confirmed`), it is one more button, and it is the honest answer
   for an operator who wants to look at those collections in Plex first. It is a product decision,
   not a safety one.
3. **Does `docs/guides.md` walk through this button?** Not checked. If it does, it needs the extra
   step.

---

# Item 2 — `wizard-dataloss`

> Audit line: _"`step-customize.tsx` — Back then Save overwrites saved settings."_

## 1. What is actually wrong

**The premise is correct, and the mechanism is not "stale form default vs saved value" within one
mount — it is that the component never reads the saved value at all.**

`web/src/pages/setup/step-customize.tsx:29-31`:

```tsx
const [choice, setChoice] = useState<TemplateChoice>("static");
const [customTpl, setCustomTpl] = useState("✨ Fresh picks");
const [rowSize, setRowSize] = useState(ROW_SIZE_DEFAULT);
```

Three hardcoded initialisers. There is no `useSettings()`, no `useEffect`, no seed. The component
does not import `@/lib/queries` at all (`step-customize.tsx:1-16`).

The step unmounts and remounts on every navigation. `web/src/pages/setup/index.tsx:45` does
`const Step = STEP_COMPONENTS[wizard.step]` and renders `<Step … />` at a fixed position
(`index.tsx:81-86`). Moving between steps swaps the _component type_ at that position, which React
handles by unmounting the old tree and mounting the new one — every `useState` initialiser re-runs.
`useWizard` (`lib/wizard.ts:189-195`) only persists the step index and the `WizardData` blob; the
row name and size live in `settings`, not in `WizardData` (`lib/wizard.ts:19-36` — `customized?:
boolean` is the only trace of this step, a bare "was it visited" flag).

The full sequence:

1. Step 6. The owner picks Custom, types `🍿 Movie night`, sets size to 25, presses **Save &
   continue**. `save.mutate()` (`step-customize.tsx:41-51`) PUTs
   `{"row.name_template": "🍿 Movie night", "row.size": 25}`. Both are now in the DB.
2. Step 7 (First run). They press **Back** (`index.tsx:102-105`).
3. `StepCustomize` mounts fresh: `choice = "static"`, `rowSize = 15`.
4. The screen now shows the classic card selected and 15 titles — **their saved values are not on
   screen anywhere.**
5. They press **Save & continue** (or, worse, **Skip for now — you can change this later**, whose
   own comment at `step-customize.tsx:170-172` promises "a name or size the user typed is never
   silently dropped by taking the quick path out"). The PUT sends
   `{"row.name_template": "✨ {library_name} Picked for You", "row.size": 15}`.
6. `🍿 Movie night` and size 25 are gone, with no warning and no visible change at the moment of
   loss — the screen looked like that before the click too.

Steps 5 and 7 are also the _most_ likely steps to be revisited: step 7 is where the first run
happens and where an operator goes back to change the row name after seeing it rendered.

**`PUT /api/settings` is not at fault** — it writes exactly the keys it is sent, which is correct.
The component sends the wrong values.

**The sibling step already has the fix.** `web/src/pages/setup/step-history.tsx:31-44`:

```tsx
// Re-entering this step (Back/Next) remounts it, so seed the fields from what's already saved,
// once, when settings arrive. Keys come back redacted as "•••••" — that's what shows, and the
// backend treats a re-sent "•••••" as "no change", so nothing gets clobbered on save.
const settings = useSettings();
const seeded = useRef(false);
useEffect(() => { … });
```

So this is not an unknown hazard in this codebase — it is one that was found, fixed, and commented
in `step-history.tsx`, and `step-customize.tsx` never received the same treatment. That makes the
correct fix a matter of applying an established local pattern, not inventing one.

**A detail that matters for risk:** the hardcoded initialisers happen to equal the server defaults —
`settings_store.py:28-29` has `"row.name_template": "✨ {library_name} Picked for You"` (identical to
`STATIC_TPL`, `step-customize.tsx:18`) and `"row.size": 15` (identical to `ROW_SIZE_DEFAULT`,
`lib/constants.ts:14`). So on a **first** pass through a fresh install the seed is a no-op and the
fix changes nothing. The bug is reachable only on **re-entry after a save**, which is precisely why
it survived: the e2e wizard walk-through (`tests/e2e/test_wizard_e2e.py:150-176`) goes forward only
and asserts `settings["row.size"] == 10` at the end (line 201) — it never presses Back.

## 2. The fix

Cause, not symptom: **read the saved settings and seed the local state from them, once, before the
step can write.** Two halves, and the second is not optional.

### 2a. Seed from what is saved (the `step-history.tsx` pattern)

```tsx
import { useEffect, useId, useRef, useState } from "react";
…
import { settingString, settingNumber, renderRowName } from "@/lib/format";
import { clampRowSize, ROW_SIZE_DEFAULT } from "@/lib/constants";
import { useSettings } from "@/lib/queries";
```

```tsx
export function StepCustomize({ update, next }: StepProps) {
  const [choice, setChoice] = useState<TemplateChoice>("static");
  const [customTpl, setCustomTpl] = useState("✨ Fresh picks");
  const [rowSize, setRowSize] = useState(ROW_SIZE_DEFAULT);
  const customId = useId();

  // Re-entering this step (Back, then Save or Skip) remounts it — index.tsx swaps the component at
  // a fixed position, so every useState initialiser re-runs. Without this seed the three fields
  // came back at their hardcoded defaults while the DB still held what was saved a moment ago, and
  // the next Save silently PUT the defaults over it. The same fix, for the same reason, as
  // step-history.tsx:31-44.
  const settings = useSettings();
  const seeded = useRef(false);
  useEffect(() => {
    const saved = settings.data;
    if (seeded.current || !saved) return;
    seeded.current = true;
    const template = settingString(saved, "row.name_template");
    if (template === STATIC_TPL) setChoice("static");
    else if (template === DYNAMIC_TPL) setChoice("dynamic");
    else if (template) {
      // Anything that is neither preset IS the custom option — including a template written in
      // Settings rather than here. Round-tripping it is what stops this step overwriting a name the
      // wizard never offered.
      setChoice("custom");
      setCustomTpl(template);
    }
    // Not a functional updater, unlike step-history's string fields. `rowSize` starts at a real
    // number, so "did the user touch it?" is unanswerable from its value — 15 typed by hand and 15
    // never touched are the same state. The Save gate below closes that race instead: nothing can
    // be typed before the seed lands, because nothing can be SAVED before it lands.
    setRowSize(clampRowSize(settingNumber(saved, "row.size", ROW_SIZE_DEFAULT)));
  }, [settings.data]);
```

`RowSizeField` picks the seeded value up correctly without further work: it re-syncs its own text
buffer during render when `value` changes from outside (`components/row-size-field.tsx:29-33`).

### 2b. Do not let the step WRITE before it has READ

The seed alone leaves a narrow but real race: `GET /api/settings` is in flight, the owner clicks
**Save & continue** immediately, and the PUT carries the unseeded defaults — the identical data loss,
in a smaller window. Close it by gating the write on the read:

```tsx
// A write before the read has landed is the same overwrite in a smaller window. `settings.isError`
// stays disabled deliberately: we could not read what is saved, so we must not write over it —
// and GET /api/settings is a local DB read, so a failure here means the server is unreachable and
// the PUT would fail anyway. Nothing is stranded: Next in the footer still moves on without
// saving (index.tsx:94), and everything on this screen is editable later in Settings.
const ready = settings.isSuccess;
```

```tsx
  <Button onClick={() => save.mutate()} disabled={!ready || save.isPending}>
  …
  <Button variant="ghost" onClick={() => save.mutate()} disabled={!ready || save.isPending}>
```

and the error state the frontend rules require (`.claude/rules/frontend.md`, "every data view
handles all four states"):

```tsx
{
  settings.isError && (
    <p role="alert" className="text-sm text-destructive-text">
      Couldn’t read your current settings, so this step won’t save over them.{" "}
      <button
        type="button"
        className="underline underline-offset-2"
        onClick={() => void settings.refetch()}
      >
        Try again
      </button>
      , or press Next — you can set the row name in Settings later.
    </p>
  );
}
```

### 2c. Deliberately NOT done

- **No change to `WizardData`.** Adding `row_name`/`row_size` to the wizard blob would give this
  step a second source of truth alongside `settings`, and the two would drift the moment somebody
  edits the row name in Settings mid-setup. `settings` is where the value lives; the step should
  read it.
- **No change to `PUT /api/settings`.** Sending only the keys you mean is the correct contract.
- **No change to `useWizard`.** The step index and the `WizardData` blob it persists are not
  implicated.

## 3. Tests, written first

New file `web/src/test/step-customize.test.tsx`, harness copied from
`web/src/test/step-history.test.tsx:1-47` (which mocks `@/lib/api` and wraps in a
`QueryClientProvider`).

**Fails against today's code — this is the bug, stated as a test:**

```tsx
it("does not overwrite a saved row name and size when the step is re-entered", async () => {
  // Back from First run remounts this step (index.tsx swaps the component at a fixed position), and
  // its useState initialisers used to win over what was in the database. Save then PUT the defaults
  // over a name the owner had already saved, with nothing on screen changing at the moment of loss.
  getSettings.mockResolvedValue({
    "row.name_template": "🍿 Movie night",
    "row.size": 25,
  });
  renderStep();

  await screen.findByDisplayValue("🍿 Movie night"); // the seed landed
  expect(screen.getByLabelText("How many titles")).toHaveValue(25);

  await userEvent.click(
    screen.getByRole("button", { name: /^Save & continue$/ }),
  );

  // The kwargs, not the call count: a Save that fires with the wrong body is exactly the bug.
  expect(putSettings).toHaveBeenLastCalledWith({
    "row.name_template": "🍿 Movie night",
    "row.size": 25,
  });
});
```

**Fails against today's code** — and it is the one that matters most, because this button's own
source comment promises it:

```tsx
it("Skip for now does not silently drop a name saved earlier", async () => {
  // step-customize.tsx:170-172 says Skip saves "so a name or size the user typed is never silently
  // dropped by taking the quick path out". On re-entry it did the opposite: it wrote the classic
  // default over a custom name, under a label that says nothing will change.
  getSettings.mockResolvedValue({
    "row.name_template": "🍿 Movie night",
    "row.size": 25,
  });
  renderStep();
  await screen.findByDisplayValue("🍿 Movie night");

  await userEvent.click(screen.getByRole("button", { name: /Skip for now/ }));

  expect(putSettings).toHaveBeenLastCalledWith({
    "row.name_template": "🍿 Movie night",
    "row.size": 25,
  });
});
```

**Fails against today's code** (today the Save button is live immediately):

```tsx
it("cannot save before it has read what is already saved", async () => {
  // A PUT before the GET lands is the same overwrite in a smaller window.
  let resolve!: (value: Record<string, unknown>) => void;
  getSettings.mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  renderStep();

  expect(
    screen.getByRole("button", { name: /^Save & continue$/ }),
  ).toBeDisabled();
  expect(screen.getByRole("button", { name: /Skip for now/ })).toBeDisabled();

  resolve({ "row.name_template": "🍿 Movie night", "row.size": 25 });
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /^Save & continue$/ }),
    ).toBeEnabled(),
  );
});
```

**Fails against today's code** (the seed selects the preset card):

```tsx
it("re-selects the preset card the saved template came from", async () => {
  // A saved template that IS one of the presets must come back as that preset, not as Custom with
  // the preset's text in the box — otherwise the next save writes the same string through a
  // different door and the card the owner picked looks unchosen.
  getSettings.mockResolvedValue({
    "row.name_template": "Because you watched {top_seed}",
    "row.size": 15,
  });
  renderStep();

  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /Because you watched/ }),
    ).toHaveAttribute("aria-pressed", "true"),
  );
  expect(screen.queryByLabelText("Custom row name")).toBeNull();
});
```

**PASSES against today's code, and must keep passing** — the first-run case, which is the whole
reason this fix is low risk:

```tsx
it("leaves a fresh install on the classic defaults", async () => {
  // settings_store.py:28-29 seeds exactly STATIC_TPL and 15, so on a first pass the seed is a
  // no-op — the fix cannot change what the wizard does for a new user.
  getSettings.mockResolvedValue({
    "row.name_template": "✨ {library_name} Picked for You",
    "row.size": 15,
  });
  renderStep();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: /^Save & continue$/ }),
    ).toBeEnabled(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: /^Save & continue$/ }),
  );

  expect(putSettings).toHaveBeenLastCalledWith({
    "row.name_template": "✨ {library_name} Picked for You",
    "row.size": 15,
  });
});
```

### An e2e addition worth having

`tests/e2e/test_wizard_e2e.py` walks the wizard forward only, which is why nothing caught this. Add
a Back-then-Save leg to the existing walk-through, after the "Save & continue" at line 176:

```python
    # Back into "Make it yours" and straight out again — the step remounts, and it used to come
    # back on its hardcoded defaults with the DB still holding what was just saved, so this second
    # Save wrote 15 titles and the classic name over the 10 and the {top_seed} template above.
    page.get_by_role("button", name="Back").click()
    expect(page.get_by_role("heading", name="Make it yours")).to_be_visible()
    expect(page.get_by_label("How many titles")).to_have_value("10")
    page.get_by_role("button", name="Save & continue").click()
    expect(page.get_by_role("heading", name="First run")).to_be_visible(timeout=LOAD)
```

The existing assertions at `test_wizard_e2e.py:200-202` (`settings["row.size"] == 10`,
`settings["row.name_template"] == "Because you watched {top_seed}"`) then become the real proof,
end to end, against the fake PMS — and they **fail today** with that leg inserted.

## 4. What could regress, and which test catches it

| Regression                                                         | Caught by                                                                                                                            |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| Seed runs more than once and clobbers what the owner just typed    | `seeded` ref; add an "edits survive a settings refetch" test if `useSettings` ever gains a refetch interval                          |
| Seed maps a preset template to Custom (so the card looks unchosen) | `re-selects the preset card the saved template came from`                                                                            |
| Save fires before the read lands                                   | `cannot save before it has read what is already saved`                                                                               |
| A fresh install now behaves differently from before                | `leaves a fresh install on the classic defaults`, and the whole `test_wizard_e2e.py` walk-through                                    |
| `row.size` seeded out of the 5–40 range from a hand-edited DB      | `clampRowSize` (`lib/constants.ts:17-20`); worth one test with `"row.size": 999` asserting `40`                                      |
| A settings read failure strands the wizard                         | the error state renders a retry, and footer **Next** still advances (`index.tsx:94`) — assert `Next` is enabled in an `isError` test |

## 5. Settings / migration / API / UI impact

- **Settings:** none added. This step now _reads_ `row.name_template` and `row.size`, which it
  already writes.
- **Migration:** none. **API:** none. **OpenAPI snapshot:** untouched.
- **UI:** the step briefly has both Save buttons disabled while `GET /api/settings` is in flight
  (a local DB read — milliseconds), and gains an error+retry line for the case where that read
  fails. On a fresh install the visible behaviour is unchanged.
- **e2e:** the wizard spec changes (see above), so `pytest -m e2e` is required before commit per
  `.claude/CLAUDE.md`.
- **Docs:** none — no user-visible option is added or removed.

## 6. Open questions

1. **Do the other wizard steps have the same hole?** `step-history.tsx` and `step-curator.tsx` both
   call `useSettings()` and look fine; `step-connect.tsx`, `step-users.tsx` and `step-welcome.tsx`
   were not audited for this. Worth a five-minute sweep as part of the same commit — not designed
   here, because I did not read them.
2. **Should the seed be a shared hook?** Three steps now do "seed once from settings on mount"
   (`step-history`, `step-curator`, `step-customize`). A `useSeededSettings` helper would stop the
   fourth from forgetting. Against: two of the three seed differently (redacted secrets vs a
   derived radio choice), so a shared hook risks being an abstraction over two things that only
   look alike. Flagged as a judgement call for the implementer.
3. **Should `RowSizeField` be gated too, so it does not paint 15 and then jump to 25?** The seed
   arrives in an effect, so there is one frame at the default. `RowSizeField` corrects during
   render rather than in an effect (`row-size-field.tsx:26-33`), so the jump should not be visible —
   but that is reasoning about layout, not a measurement, so it should be looked at in the browser
   rather than trusted.

---

# Item 3 — `dry-run-gap`

> Audit line: _"`PATCH`/`DELETE /collections/{id}` expose no `dry_run`."_

## 1. What is actually wrong

**The fact is right; the framing "violates plex-safety rule 8" is too strong, and getting that
straight changes what the fix should be.**

Verified:

- `PATCH /collections/{collection_id}` — `shortlist/server/api/collections.py:942-1105`. Its body is
  `CollectionIn` (`collections.py:131-180`), which has no `dry_run` field.
- `DELETE /collections/{collection_id}` — `collections.py:1188-1238`. It takes no parameters at all
  beyond the path id.

Rule 8 reads: _"Every write path takes `dry_run` and logs the would-be diff instead."_ Neither of
these handlers writes to Plex. They both **enqueue** a durable `row.reconcile` job
(`_queue_reconcile`, `collections.py:912-940`), and the code that actually touches Plex —
`_reconcile_row_removal` (`services/collection_reconcile.py:323-424`) — takes `dry_run`, ORs
`SHORTLIST_DRY_RUN` in at the chokepoint (line 366), logs the would-be diff, and audits the
_effective_ value rather than the requested one. The job handler `_row_reconcile`
(`services/jobs.py:1365-1402`) reads `payload.get("dry_run", False)` and threads it through. So the
write path complies with rule 8 exactly as written.

**The gap is at the API surface, and it is uneven rather than absent:**

| Endpoint                         | `dry_run`? | Where                                          |
| -------------------------------- | ---------- | ---------------------------------------------- |
| `POST /collections/{id}/cleanup` | yes        | `CleanupRequest`, `collections.py:1337-1338`   |
| `POST /collections/{id}/rename`  | yes        | `RenameRequest.dry_run`, `collections.py:1247` |
| `PATCH /collections/{id}`        | **no**     | `collections.py:942`                           |
| `DELETE /collections/{id}`       | **no**     | `collections.py:1188`                          |

Two of the four doors onto a row's Plex collections preview; two do not. That inconsistency is the
real finding, and it matters differently for each:

**DELETE is mostly covered already, but not entirely.** `POST /{id}/cleanup {dry_run: true}` calls
`reconcile.run_reconcile(dry_run=True)` and returns the exact list of collections a delete would
strip — and the SPA already uses it that way
(`components/rows/row-destructive-actions.tsx:44-46`, whose confirm button is _disabled_ if the dry
run failed, lines 172-178, "the one thing plex-safety rule 8 exists to prevent"). What `/cleanup`
cannot show is the **local** half of a delete, and one part of it is a genuine surprise:

- `_forget_anchor_row` (`collections.py:526-548`) walks **every other row** and silently strips any
  `hub_anchor` entry that positioned it relative to the row being deleted. Deleting row A therefore
  changes where rows B and C appear on the shelf. It is logged (`collections.py:1219-1226`) _after_
  the fact and nothing warns beforehand.
- A `shared` row additionally queues a server-wide `privacy.sync` (`collections.py:1228-1232`),
  recomputing every account's `label!=` excludes.
- The row's cron schedule is deregistered (`collections.py:1229`).

**PATCH has no preview anywhere, and it can delete.** `plan_row_changes`
(`api/row_changes.py:105-189`) can emit a `RECONCILE` whose `in_sections` names libraries the row
walked away from — narrowing a row's media from `both` to `movie`, or dropping a library key,
**removes that row's collections from those libraries** (`_stranded_sections`,
`collections.py:886-910`). Shrinking a row's audience removes those users' copies
(`only_user_ids`). Flipping `build` removes the old build's collections wholesale. None of that is
previewable today, and the row editor is the screen these edits are made from.

So: **the audit found the right two endpoints, but the urgent one is PATCH, not DELETE, and the
reason is not rule 8's letter — it is that a narrowing edit deletes collections with no way to ask
what it would take.**

### The landmine in the obvious implementation

The natural way to add `dry_run` to PATCH is "run the handler, compute the plan, then
`session.rollback()`". **That does not work here, and would ship a preview that writes.**
`SettingsStore.set` commits internally (`shortlist/server/settings_store.py:373-382`, line 382 is
`self._session.commit()`), and the PATCH handler calls it at `collections.py:1021` to write
`row.name_template` when the default row is renamed. A rollback at the end of the handler would not
undo that: a "preview" of renaming the default row would permanently change the global row name
template for every row on the server. `session.add(Event(...))` for a poster change
(`collections.py:1049-1059`) is inside the outer transaction and _would_ roll back, but relying on
"which writes happen to be in the outer transaction" is exactly the assumption that ages badly.

## 2. The fix

Three parts. They are independent and can land separately.

### 2a. PATCH — `dry_run` that PROJECTS the edit rather than applying and rolling it back

Add to `CollectionIn` (`collections.py`, beside `defer_rename` at line 172):

```python
    #: Preview only: validate the edit, work out what it would owe Plex, and write NOTHING —
    #: not the row, not the audience, not `row.name_template`, not the job queue (rule 8).
    #: Implemented by PROJECTING the post-edit snapshot rather than applying and rolling back:
    #: `SettingsStore.set` commits internally (settings_store.py:382), so a rollback at the end of
    #: this handler would leave a "preview" of a default-row rename permanently applied.
    dry_run: bool = False
```

New pure projection, beside `_snapshot` (`collections.py:1108`):

```python
def _projected_snapshot(session, collection: Collection, body: CollectionIn, sent: set[str]) -> dict:
    """What `_snapshot` WOULD return after this PATCH, computed without touching the row.

    The dry-run counterpart to `_snapshot`, and deliberately not "apply it and roll back": the
    handler's default-row rename goes through `SettingsStore.set`, which commits inside itself
    (settings_store.py:382), so a rollback would not undo it and the preview would have written.

    Every field here is one `_snapshot` reads, resolved the same way the apply path resolves it. The
    two are pinned together by `test_a_dry_run_projects_exactly_what_the_real_patch_produces`, which
    runs both over a matrix of edits and asserts they agree — a projection that drifts from the
    apply path is a preview that lies, which is worse than no preview at all.
    """
    before = _snapshot(session, collection)
    audience_kind = body.audience if "audience" in sent else collection.audience
    if audience_kind == "everyone":
        audience = frozenset(user_id for (user_id,) in session.query(User.id).all())
    elif sent & {"audience", "audience_user_ids"}:
        wanted = list(dict.fromkeys(body.audience_user_ids))
        known = {uid for (uid,) in session.query(User.id).filter(User.id.in_(wanted)).all()}
        # Same refusal as `_set_audience`, in the same words: a preview that quietly drops an
        # unknown id would report a smaller removal than the real edit performs.
        if unknown := [uid for uid in wanted if uid not in known]:
            raise HTTPException(422, f"no such user(s): {unknown} — the audience must be existing users")
        audience = frozenset(wanted)
    else:
        audience = before["audience"]
    return {
        "slug": collection.slug,
        "build": body.build if "build" in sent else before["build"],
        "enabled": body.enabled if "enabled" in sent else before["enabled"],
        "media": body.media if "media" in sent else before["media"],
        "libraries": tuple(str(k) for k in body.library_keys) if "library_keys" in sent else before["libraries"],
        "audience": audience,
        "poster_mode": (body.poster.mode or "") if "poster" in sent else before["poster_mode"],
        "show_days": tuple(_normalise_show_days(body.show_days)) if "show_days" in sent else before["show_days"],
    }
```

An early return in `update_collection`, placed **after** `_validate`, `_validate_anchor_rows`,
`_reject_duplicate_name` and `_validate_pairing` — a preview must fail on exactly the input a real
edit fails on — and **before** the first `setattr`:

```python
    if body.dry_run:
        change = _row_change(before, _projected_snapshot(session, collection, body, sent), …)
        plan = plan_row_changes(change, stranded)
        return {**_serialize(session, collection), "dry_run": True, "plan": _plan_view(state, plan, change)}
```

The `RowChange` construction at `collections.py:1073-1092` moves into a small `_row_change(before,
after, *, template_before, template_after, defer_rename)` helper so the live and dry-run paths build
it identically — one construction site, so they cannot drift.

`_plan_view` renders `PlannedWork` for a human:

```python
def _plan_view(state, plan: list[PlannedWork], change: RowChange) -> list[dict]:
    """`PlannedWork` as the would-be diff, resolving the RECONCILE entries against Plex.

    A reconcile is the only entry that DELETES, so it is the only one that pays for a real read:
    `_reconcile_row_removal(dry_run=True)` walks the same collections the live job would and returns
    the titles it would strip — the same call, at the same cost, `POST /{id}/cleanup?dry_run=true`
    already makes. The other kinds are declarative and need no read.
    """
    view: list[dict] = []
    for work in plan:
        if work.kind != RECONCILE:
            view.append({"kind": work.kind, "reason": work.scope, "collections": []})
            continue
        removed: list[str] = []
        _reconcile_row_removal(
            state,
            slug=change.slug,
            build=change.build_before,
            dry_run=True,
            removed=removed,
            only_user_ids=set(work.only_user_ids) if work.only_user_ids is not None else None,
            in_sections=set(work.in_sections) if work.in_sections is not None else None,
        )
        view.append({"kind": work.kind, "reason": work.scope, "collections": removed})
    return view
```

`_reconcile_row_removal` is the right entry point, not `run_reconcile`: `run_reconcile`
(`collection_reconcile.py:491-516`) does not accept `in_sections` or `template`, and `in_sections`
is exactly what a _narrowing_ preview is about.

**Response shape.** `CollectionOut` is a `PassthroughModel` (`api/schemas.py:28-31`, `extra="allow"`),
so the extra keys reach the client — but an undeclared key is invisible in the OpenAPI schema, which
means the SPA's generated types would not know about it. Declare them:

```python
class CollectionOut(PassthroughModel):
    …
    #: Present only on a dry-run PATCH. The row is returned UNCHANGED; `plan` is what the edit would
    #: owe Plex, in the order it would happen.
    dry_run: bool | None = None
    plan: list[PlanEntryOut] | None = None


class PlanEntryOut(PassthroughModel):
    """One unit of Plex work an edit would cause."""

    kind: str = _closed_set_out(
        {RECONCILE, PRIVACY_SYNC, RENAME, POSTER_RESET, VISIBILITY}, "What this step would do."
    )
    reason: str
    #: The collection titles a RECONCILE would remove. Empty for every other kind.
    collections: list[str]
```

Optional-with-`None` is a deliberate exception to `_closed_set_out`'s "declare responses required so
a dropped field fails loudly" note (`collections.py:94-100`): these two keys are genuinely absent on
a live PATCH, and a required field would force a live edit to invent them.

### 2b. DELETE — `dry_run` as a query parameter

`DELETE` bodies are awkward through `fetch` and FastAPI both; a query parameter is the idiomatic
shape and works with the SPA's existing `request()` helper unchanged (`web/src/lib/api.ts:154-192`,
which already handles both 204 and a JSON body).

```python
class RowDeletePreviewOut(PassthroughModel):
    """What `DELETE /collections/{id}?dry_run=true` WOULD do. Nothing is written."""

    dry_run: bool
    #: Collection titles that would be removed from Plex, for everyone who has this row.
    collections: list[str]
    #: Slugs of OTHER rows that would lose their shelf placement, because it is positioned relative
    #: to this one. `_forget_anchor_row` clears these; nothing warns about it today.
    anchors_cleared: list[str]
    #: Whether every account's share filter would be recomputed (a shared row's label stops being
    #: declared shared, so the exclude has to come out of all of them).
    privacy_sync: bool
    #: Whether this row's cron job would stop firing.
    schedule_cleared: bool
    message: str


@router.delete("/{collection_id}", status_code=204, responses={200: {"model": RowDeletePreviewOut}})
async def delete_collection(collection_id: int, request: Request, dry_run: bool = False) -> None | Response:
    """Delete a row. `dry_run=true` reports what that would do to Plex and to the other rows, and
    writes nothing (rule 8) — returning 200 with the preview instead of 204.

    `POST /{id}/cleanup?dry_run=true` already previews the PLEX half. What only this can show is the
    local half: deleting a row silently strips every OTHER row's shelf placement that was positioned
    relative to it, which is a change to where two other people's rows appear and which nothing
    warned about before it happened.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        slug, build = collection.slug, collection.build
        template = reconcile.row_template(session, slug, state.secrets)
        if dry_run:
            # Read-only: the same walk `_forget_anchor_row` does, without the write.
            anchors = [
                row.slug
                for row in session.query(Collection).all()
                if any(
                    isinstance(e, dict) and str(e.get("row") or "").strip() == slug
                    for e in (row.hub_anchor or {}).values()
                )
            ]
            has_schedule = bool((collection.schedule or "").strip())
    if dry_run:
        removed: list[str] = []
        await run_in_threadpool(
            _reconcile_row_removal,
            state, slug=slug, build=build, dry_run=True, removed=removed, template=template,
        )
        return JSONResponse(
            {
                "dry_run": True,
                "collections": removed,
                "anchors_cleared": anchors,
                "privacy_sync": build == "shared",
                "schedule_cleared": has_schedule,
                "message": f"Would remove {len(removed)} collection(s) from Plex.",
            }
        )
    …  # the existing body, unchanged from collections.py:1198 onward
```

`_forget_anchor_row` should be refactored into a read (`_rows_anchored_to(session, slug) ->
list[str]`) plus a write that consumes it, so the preview and the real delete cannot disagree about
which rows lose their placement. That is a two-line change to `collections.py:526-548` and removes
the duplicated walk above.

### 2c. Wire the PATCH preview into the row editor's narrowing path (optional, recommended)

`components/rows/row-destructive-actions.tsx` already demonstrates the shape: dry run on open,
disable the destructive confirm if the dry run FAILED, name the count. The same treatment belongs
on the row editor's Save when the edit narrows a row's media or libraries — that is the edit that
deletes collections with no preview today. Not designed further here; it is a Wave 5 (Admin UX)
shape and should not hold up the API fix.

## 3. Tests, written first

### `tests/unit/test_api_row_changes.py` — pure, no Plex

**Passes today** (it tests the existing planner, and is the anchor for the projection test):
nothing new needed.

### `tests/integration/test_api_collections.py`

**Fails today** — `dry_run` is not a field on `CollectionIn`, so it is silently ignored and the
edit applies:

```python
def test_a_dry_run_patch_writes_nothing(client, seeded_row):
    """The whole contract in one test: a preview changes neither the row nor the queue."""
    before = client.get(f"/api/collections/{seeded_row.id}").json()

    resp = client.patch(f"/api/collections/{seeded_row.id}", json={**BODY, "media": "movie", "dry_run": True})

    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert client.get(f"/api/collections/{seeded_row.id}").json() == before, "a preview must not edit"
    assert queued_jobs(client) == [], "a preview must not enqueue Plex work"
```

**Fails today, and is the one that matters** — this is the write a rollback-based implementation
would not undo:

```python
def test_a_dry_run_rename_of_the_default_row_does_not_write_the_global_template(client):
    """`SettingsStore.set` commits inside itself (settings_store.py:382), so "apply then roll back"
    would leave this permanently applied — a preview that renames every row on the server."""
    before = client.get("/api/settings").json()["row.name_template"]

    client.patch(f"/api/collections/{default_row_id}", json={**BODY, "name": "🍿 Movie night", "dry_run": True})

    assert client.get("/api/settings").json()["row.name_template"] == before
```

**Fails today** — the plan is not returned at all:

```python
def test_a_dry_run_patch_names_the_libraries_a_narrowing_would_strip(client, seeded_row, fake_plex):
    """Narrowing `both` to `movie` DELETES the row's collections in the libraries it walks away
    from (`_stranded_sections`). That is the edit with the most destructive reach and the only one
    of the four collection doors with no preview at all."""
    resp = client.patch(f"/api/collections/{seeded_row.id}", json={**BODY, "media": "movie", "dry_run": True})

    plan = resp.json()["plan"]
    assert [entry["kind"] for entry in plan] == ["reconcile", "privacy_sync"]
    # The kwargs the SUT controls, not just "a plan came back": the titles are the whole point.
    assert plan[0]["collections"] == ["✨ TV Shows Picked for You"]
```

**Fails today** — DELETE takes no parameters:

```python
def test_a_dry_run_delete_keeps_the_row_and_names_what_it_would_take(client, seeded_row):
    resp = client.delete(f"/api/collections/{seeded_row.id}?dry_run=true")

    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert client.get(f"/api/collections/{seeded_row.id}").status_code == 200, "a preview must not delete"
    assert queued_jobs(client) == []


def test_a_dry_run_delete_warns_that_other_rows_lose_their_placement(client, seeded_row, anchored_row):
    """`_forget_anchor_row` silently reparents every row positioned relative to this one. It is
    logged AFTER the fact and nothing warns first — this is the half `/cleanup?dry_run=true`
    cannot show, and the reason DELETE needs its own preview rather than a pointer at cleanup."""
    resp = client.delete(f"/api/collections/{seeded_row.id}?dry_run=true")

    assert resp.json()["anchors_cleared"] == [anchored_row.slug]
    # And the warning must be true: the real delete does exactly this.
    client.delete(f"/api/collections/{seeded_row.id}")
    assert client.get(f"/api/collections/{anchored_row.id}").json()["hub_anchor"] == {}
```

**The drift test — the most important one here.** A projection that disagrees with the apply path is
a preview that lies:

```python
@pytest.mark.parametrize(
    "patch",
    [
        {"media": "movie"},
        {"library_keys": ["1"]},
        {"build": "shared"},
        {"enabled": False},
        {"audience": "subset", "audience_user_ids": [1]},
        {"audience": "everyone"},
        {"show_days": [1, 3, 5]},
        {"poster": {"mode": ""}},
        {"name": "🍿 Movie night"},
        {"size": 25},                       # owes Plex nothing — the empty-plan cell
    ],
)
def test_a_dry_run_projects_exactly_what_the_real_patch_produces(client, seeded_row, patch):
    """`_projected_snapshot` computes the post-edit state WITHOUT applying it, so it can drift from
    the apply path field by field and nothing would notice — the preview would simply be wrong about
    a delete. Runs both over the matrix `plan_row_changes` branches on (`.claude/rules/testing.md`:
    cover the matrix, not one cell) and asserts they agree."""
    preview = client.patch(f"/api/collections/{seeded_row.id}", json={**BODY, **patch, "dry_run": True}).json()
    kinds_previewed = [entry["kind"] for entry in preview["plan"]]

    client.patch(f"/api/collections/{seeded_row.id}", json={**BODY, **patch})

    assert kinds_previewed == kinds_actually_queued(client)
```

### `tests/unit/test_openapi_snapshot.py`

Both of its tests **fail** the moment the schema changes, by design, until
`web/openapi.snapshot.json` is regenerated. That is the reminder, not a bug.

## 4. What could regress, and which test catches it

| Regression                                                                                                 | Caught by                                                                                                                                                                                      |
| ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The projection drifts from the apply path, so a preview under-reports a deletion                           | `test_a_dry_run_projects_exactly_what_the_real_patch_produces` over the full matrix                                                                                                            |
| A "preview" writes the global `row.name_template` (the rollback trap)                                      | `test_a_dry_run_rename_of_the_default_row_does_not_write_the_global_template`                                                                                                                  |
| A preview enqueues a real `row.reconcile`                                                                  | `queued_jobs(client) == []` in both dry-run tests                                                                                                                                              |
| `dry_run` becomes a stored column because someone adds it to `_PATCHABLE_COLUMNS`                          | `test_a_dry_run_patch_writes_nothing` (the row must round-trip byte-identical)                                                                                                                 |
| A dry-run PATCH skips a validation the real one enforces, so the preview succeeds where the edit would 422 | add one cell to the matrix with a duplicate name and assert both return 422                                                                                                                    |
| DELETE's real path changes but the preview's anchor walk does not                                          | the second half of `test_a_dry_run_delete_warns_that_other_rows_lose_their_placement`, which runs the real delete straight after — and the `_rows_anchored_to` refactor, which leaves one walk |
| `SHORTLIST_DRY_RUN` safe mode is bypassed by the new path                                                  | `_reconcile_row_removal` ORs it in at its own chokepoint (`collection_reconcile.py:366`); the preview passes `dry_run=True`, which the chokepoint can only strengthen                          |
| The SPA's generated types do not know about `plan`                                                         | `tests/unit/test_openapi_snapshot.py` (schema drift), plus `tsc -b` once `gen:api` is re-run                                                                                                   |

## 5. Settings / migration / API / UI impact

- **Settings:** none. **Migration:** none — no schema change. `dry_run` is a request field and a
  query parameter, never a column.
- **API — this is the real surface change:**
  - `CollectionIn` gains `dry_run: bool = False` (additive; every existing client is unaffected).
  - `CollectionOut` gains optional `dry_run` and `plan`.
  - New response model `PlanEntryOut`, new response model `RowDeletePreviewOut`.
  - `DELETE /collections/{id}` gains a `dry_run` query parameter and a documented 200 response
    alongside its 204.
- **OpenAPI snapshot:** `web/openapi.snapshot.json` MUST be regenerated (the command is in
  `tests/unit/test_openapi_snapshot.py`'s module docstring), then `pnpm -C web gen:api`. Both
  snapshot tests fail until this is done.
- **UI:** none required by this item. §2c is a follow-on.
- **Docs:** `docs/reference.md` documents endpoint signatures and must gain the `dry_run` parameter
  on both endpoints and the preview response shapes, in the same PR (`.claude/rules/docs.md`).
- **Architecture Review:** required. This touches a code path that writes to Plex, per
  `.claude/CLAUDE.md`'s risk list. It is the strongest reason the three items in this document
  should be reviewed as one Wave 1 diff rather than separately.

## 6. Open questions

1. **Should a dry-run PATCH really pay for a Plex walk?** `_plan_view` calls
   `_reconcile_row_removal(dry_run=True)`, which walks every user across every library — the same
   cost `/cleanup?dry_run=true` already pays, but the row editor may call PATCH often. An
   alternative is a `deep: bool = False` flag: plan kinds always, collection titles only on request.
   Not decided here.
2. **Is `_reconcile_row_removal` safe to call with `dry_run=True` from a request thread?** It calls
   `state.run_service.build_context(dry_run=True, plex_only=True)` and is documented "Runs in an
   executor" (`collection_reconcile.py:359`). The sketch above wraps the DELETE preview in
   `run_in_threadpool`; the PATCH sketch does not, and should. Whether `build_context` is safe to
   call concurrently with a run holding the writer lock was **not verified** — it must be, before
   implementation.
3. **Does a dry-run PATCH need `_stranded_sections`' Plex read, and what does it report when Plex is
   unreachable?** `_stranded_sections` returns an empty set on any exception
   (`collections.py:899-902`) — correct for a live edit ("not knowing which libraries exist must
   mean delete nothing"), but in a preview it silently reports "this edit deletes nothing" when the
   truth is "I could not find out". A preview must distinguish those. Probably needs a third state
   surfaced in the response; not designed here.
4. **Should `PATCH` refuse `dry_run` together with `defer_rename`?** They are both
   "caller is going to do something else with this" flags and their interaction is unexamined.
5. **Is `hub_anchor`'s `anchor` (a foreign collection title) affected by a delete too?**
   `_forget_anchor_row` only matches `entry.get("row")`, so title-anchored placements survive. That
   looks right, but I did not verify it against the engine's placement resolver.

---

# Sequencing and rollback

**Land in this order,** cheapest and most independent first:

1. `wizard-dataloss` — UI + one new web test file + an e2e leg. No API surface, no Plex path.
2. `oneclick-delete` — UI only, no dependency change, no API surface.
3. `dry-run-gap` — the only one with an API surface, an OpenAPI snapshot regeneration and a
   mandatory Architecture Review.

**One Architecture Review for the wave**, on the combined staged diff, as
`audit-2026-09-programme.md:264-270` records the owner asking for. Items 1 and 2 would not earn one
alone (UI-only, per `.claude/CLAUDE.md`'s skip list); item 3 mandates one, and reviewing the wave
together is cheaper than reviewing item 3 in isolation.

**Rollback** is per item and clean: item 1 is one `useState` and one `<Dialog>` in `jobs.tsx`; item 2
is one `useEffect` and one `disabled` clause in `step-customize.tsx`; item 3 is additive across
`collections.py` plus a regenerated snapshot, and reverting it restores today's endpoints exactly —
no data written by the new code needs undoing, because the new code writes nothing.

**Verification, with zero real-server risk:**

```
pytest tests/unit/test_openapi_snapshot.py tests/integration/test_api_collections.py -q
cd web && ./node_modules/.bin/tsc -b && ./node_modules/.bin/vitest run \
  src/test/jobs-page.test.tsx src/test/step-customize.test.tsx
pytest -m e2e -k wizard
```

Then the full `pytest`, the full `vitest run`, and `vite build` before the commit, per
`.claude/CLAUDE.md`. Nothing here needs SFLIX or any real Plex server; everything is mock- or
fake-backed.

---

## Files read for this design (read-only, no edits)

`web/src/pages/jobs.tsx` (1-36, 320-430, 690-770, 830-860), `web/src/test/jobs-page.test.tsx`
(340-445), `web/src/components/rows/row-destructive-actions.tsx` (whole),
`web/src/components/rows/row-enable-toggle.tsx` (1-70), `web/src/components/jobs/backup-panel.tsx`
(grep), `web/src/components/jobs/job-row.tsx` (100-145), `web/src/components/ui/dialog.tsx` (whole),
`web/src/components/ui/button.tsx` (9-20), `web/package.json`, `web/node_modules/@radix-ui/`,
`web/src/pages/setup/step-customize.tsx` (whole), `web/src/pages/setup/step-history.tsx` (20-80),
`web/src/pages/setup/index.tsx` (whole), `web/src/pages/setup/step-props.ts`, `web/src/lib/wizard.ts`
(whole), `web/src/lib/api.ts` (154-200, 280-300, 470-480, 600-620), `web/src/lib/queries.ts`
(236-237, 330-393), `web/src/lib/format.ts` (140-180), `web/src/lib/constants.ts` (1-20),
`web/src/components/row-size-field.tsx` (1-60), `web/src/test/step-history.test.tsx` (1-70),
`shortlist/server/api/collections.py` (58-180, 379-395, 526-560, 739-775, 886-1105, 1108-1250,
1337-1375), `shortlist/server/api/row_changes.py` (1-120), `shortlist/server/api/schemas.py` (1-31),
`shortlist/server/services/jobs.py` (855-960, 1355-1402), `shortlist/server/services/collection_reconcile.py`
(323-520), `shortlist/server/settings_store.py` (28-29, 373-398), `tests/unit/test_openapi_snapshot.py`,
`tests/e2e/test_wizard_e2e.py` (150-215), and directory listings of `tests/unit/`,
`tests/integration/`, `tests/e2e/`, `web/src/components/ui/`, `web/src/test/`.
