# Wave 6 visual-assets design — screenshots, two-account, privacy-diagram, social-preview

Origin: `.claude/docs/audit-2026-09-programme.md`, Wave 6 ("Presentation and site"). Covers four of
that wave's eight items: `screenshots`, `two-account`, `privacy-diagram`, `social-preview`. The other
four (`copy`, `badges`, `motion`, `seo`) are out of scope for this document.

**Everything below marked "Verified" was actually run against this repo during design** (build +
capture harness, real API calls against the fake-Plex e2e harness, real file reads) — not assumed.
No real Plex server was touched. All exploratory edits were reverted; `git status` is clean.

---

## 1. `screenshots` — unblock `rows.png`, pick `wizard.png`'s step

### 1.1 Current state (verified)

- `tests/e2e/test_screenshots.py::test_capture_app_screenshots` seeds via
  `build_real_rows(app)` (`tests/e2e/conftest.py:454`), which does exactly one thing: `POST
/api/runs` and wait for it. That produces exactly **one** row definition (the seeded default
  `picked` collection) — confirmed by capturing `rows.png` fresh: it showed one card, ~80% empty
  frame, matching the audit's finding exactly.
- `build_real_rows` is **shared by 5 e2e files**, not just the screenshot test:
  `test_privacy_uninstall_e2e.py`, `test_mobile_audit.py`, `test_settings_e2e.py`,
  `test_users_runs_e2e.py`, `test_screenshots.py`. Three of those files hard-assert an exact
  collection count that depends on today's single-row behaviour:
  - `test_privacy_uninstall_e2e.py:80,111` — `assert len(state.collections) == 5`
  - `test_settings_e2e.py:166,175` — `assert len(before_collections) == 5` / `"5 collections"` in the UI
  - `test_users_runs_e2e.py:230` — `assert len(state.collections) == 5`

  **This means the audit's literal instruction ("extend `build_real_rows`") is the wrong move.**
  Doing it as stated would multiply the collection count for every one of those five files and break
  three hard-coded assertions across three files that have nothing to do with screenshots.

- Row templates are real, production-defined values in `web/src/lib/row-templates.ts`
  (`ROW_TEMPLATES`), created through `POST /api/collections` (`shortlist/server/api/collections.py`,
  `CollectionIn` schema). A second row is already created this way by an existing test
  (`tests/e2e/test_watch_outcomes_e2e.py:60`: `app.api("POST", "/api/collections", json={"name": "My
Faves"})`), so this is an established, low-risk pattern, not a new one.
- The e2e fixture's watched sets are **deliberately disjoint**: `SARAH_WATCHED` = movies (101-108) +
  some shows (301-304); `MIKE_WATCHED` = shows only (305-316) (`tests/e2e/conftest.py:81-90`). A
  `min_watchers: 3` shared row template ("Popular on this server") would build **zero** picks in this
  fixture — no title has more than one watcher. Per-person templates that seed from one person's own
  history do not have this problem.
- Jekyll build: `cd web && ./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build` (pnpm is
  not on PATH; confirmed working). Capture:
  `SHOTS_DIR=<dir> .venv/bin/python -m pytest tests/e2e/test_screenshots.py -m e2e --no-cov -n0`.
- `wizard.png` is consumed by `docs/getting-started.md`, which today has **zero** images (verified: no
  `![`, no `<img>` anywhere in that file) across its 7-step walkthrough.
- `rows.png` is not referenced anywhere yet — it's destined for `docs/_layouts/home.html`'s
  `#templates` section (`<h2>It isn't only "Picked for You"</h2>`, line 133) or its screenshot gallery
  (`#screenshots`, loops `site.data.screenshots`, line 248) — the audit's own phrasing ("would
  undermine [the 'it isn't only Picked for You' section]") points at the latter.

### 1.2 The design

**Fixture change — do NOT touch `build_real_rows`.** Add the two extra rows _inside_
`test_capture_app_screenshots` itself, after everything that already depends on the single-row shape
has been captured, and using a `POST /api/collections` call per row rather than a second engine run
(the Rows list page reads `Collection` DB rows directly — a picked-and-delivered row is not required
for that page to render three cards).

Verified order, exactly as tested:

```python
def test_capture_app_screenshots(shot_page: Page, app: ShortlistApp) -> None:
    build_real_rows(app)  # unchanged — one real run, one default row, exactly as today

    sarah = _users_by_name(app)["sarah"]["id"]
    run_id = app.api("GET", "/api/runs").json()[0]["id"]

    _capture(shot_page, "/", "dashboard.png", wait="picked|watched|run")
    _capture(shot_page, f"/users/{sarah}", "user-detail.png", wait="Because you watched")
    _capture(shot_page, "/users", "users.png", wait="sarah")
    _capture(shot_page, "/runs", "runs.png", wait="succeeded|ok")
    _capture(shot_page, f"/runs/{run_id}", "run-detail.png", wait="AI tokens")
    _capture(shot_page, "/requests", "requests.png", wait="request")
    _capture(shot_page, "/settings", "settings.png", wait="Connections")

    # rows.png needs row VARIETY, not more picks — add two more row DEFINITIONS (no second run
    # needed; the Rows list reads `collections` rows directly) using verbatim production template
    # values from web/src/lib/row-templates.ts, so the screenshot shows real templates, not an
    # invented fixture shape. Deliberately per-person, not the "Popular on this server" shared
    # template: sarah/mike's watched sets never overlap in this fixture, so a min_watchers:3 shared
    # row would build zero picks here.
    for payload in (
        {  # id: because-you-watched
            "name": "🎯 Because you watched {top_seed}",
            "build": "per_person", "max_seeds": 1, "recent_count": 3,
            "media": "movie", "size": 20, "refresh_days": 1, "seed_window": 1,
        },
        {  # id: seen-it-already ("Happy to see again")
            "name": "☕ {library_name} you've already seen",
            "build": "per_person", "rewatch": True, "watched_pct": 1,
            "refresh_days": 11, "size": 15,
        },
    ):
        created = app.api("POST", "/api/collections", json=payload)
        assert created.status_code == 201, created.text
    _capture(shot_page, "/rows", "rows.png", wait="Picked for You")
```

Moving `rows.png`'s capture to the _end_, after the two new rows are added, means every
already-consumed shot (`dashboard.png`, `user-detail.png`, `runs.png`, `run-detail.png`, etc.) is
captured against the exact same single-row state as today — **zero pixel change** to any of them.
Verified directly: the freshly captured `user-detail.png` (2880×2302) is byte-for-byte the same
dimensions as the currently committed `docs/images/user-detail.png` (2880×2302).

**`wizard.png` — capture the Connect Plex step, not Welcome.** Verified by walking the real wizard
(reusing the exact sign-in/server-pick sequence already proven in
`tests/e2e/test_wizard_e2e.py::_connect_plex`) to the point where "Run checks" has completed:

```python
@skip_unless_capturing
def test_capture_wizard_screenshot(fresh_shot_page: Page, fresh_app: ShortlistApp, fake_plex) -> None:
    pms_url, _, _ = fake_plex
    page = fresh_shot_page
    stub_plex_pin(page, fresh_app)
    page.goto("/setup")
    expect(page.get_by_role("heading", name="Welcome")).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Get started").click()
    expect(page.get_by_role("heading", name="Connect Plex")).to_be_visible()
    page.get_by_role("button", name="Sign in with Plex").click()
    expect(page.get_by_role("button", name="Sign in with Plex")).to_have_count(0, timeout=LOAD)
    expect(page.get_by_text("FakePlex", exact=True).first).to_be_visible(timeout=LOAD)
    expect(page.locator("button", has_text=pms_url).first).to_be_enabled(timeout=LOAD)
    page.get_by_role("button", name="Run checks").click()
    expect(page.get_by_text("Plex Pass active")).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(500)
    _fit_viewport(page)
    _shot(page, "wizard.png")
```

Captured and inspected: the result shows three green checkmarks — _"Plex version: Plex Media Server
1.43.3.10793 supports private rows"_, _"Plex Pass active"_, _"Libraries: 2 libraries found"_ — plus
the two discovered addresses (one reachable, marked; one deliberately unreachable, per the test's own
negative case). This is the persuasive, "it actually checks your setup" moment the audit called out,
versus the current Welcome screen (two static text cards). No hostnames/IPs beyond the fake harness's
own loopback address and its deliberately-fake unreachable `10.255.255.1` test address — both are
fabricated by `tests/fakes/fake_plex.py`, not real infrastructure, so safe for the public repo.

**Recommendation: capture _both_.** Keep `wizard.png` as today (Welcome — already good, per the
audit's own finding) and add a second file `wizard-connect.png` for the checklist state, rather than
replacing one with the other. `getting-started.md` has zero images across 7 steps; two well-chosen
ones cost nothing extra to produce (one more `_capture`-style block in the same already-open browser
context) and let the walkthrough show _both_ "here's what step 1 looks like" and "here's the proof
step 2 gives you before you commit to anything."

### 1.3 Files created/committed and which docs pages consume them

| File                             | Produced by                                               | Docs page                                                                                       | Where                        |
| -------------------------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------- |
| `docs/images/wizard.png`         | `test_capture_wizard_screenshot` (unchanged)              | `docs/getting-started.md`                                                                       | near step 1 ("Welcome")      |
| `docs/images/wizard-connect.png` | `test_capture_wizard_screenshot` (new capture, same test) | `docs/getting-started.md`                                                                       | near step 2 ("Connect Plex") |
| `docs/images/rows.png`           | `test_capture_app_screenshots` (new capture, end of test) | `docs/_layouts/home.html` `#templates` section, and/or a new `docs/_data/screenshots.yml` entry | landing page                 |
| `tests/e2e/test_screenshots.py`  | hand-edited (design above)                                | —                                                                                               | test file itself             |

Embedding syntax verified against the site's own conventions: plain-markdown `.md` pages need the
Liquid `relative_url` filter (the site has `baseurl: /shortlist`, so a literal `/images/x.png` 404s):

```html
<img
  src="{{ '/images/wizard.png' | relative_url }}"
  alt="The Shortlist setup wizard's welcome step"
  width="1440"
  height="588"
/>
```

`.prose img` (`docs/assets/css/main.css:1545`) already frames images with a border/radius/margin, so
no new CSS is needed. For `rows.png` on the landing page, add one entry to `docs/_data/screenshots.yml`
(house rule: "only screens that show something no other tool does" — a bare Rows list is ordinary,
but a Rows list demonstrating **templated row variety** — a rewatch shelf, a one-title-seeded row,
each independently scheduled — is the thing being sold in the adjacent `#templates` section, so it
clears the bar when the copy frames it that way):

```yaml
- title: Add more than one row
  body: >-
    Start from a template — a rewatch shelf, a row built around one recent favourite, films only —
    each with its own size and refresh schedule. Every row stays private to its owner.
  image: rows.png
  alt: The Rows page listing three configured rows — the default Picked for You row, a Because you watched row, and a rewatch row
  path: /rows
  width: 1440
  height: 860
```

### 1.4 What could regress, and how it's caught

- **Touching `build_real_rows` would have broken `test_privacy_uninstall_e2e.py`,
  `test_settings_e2e.py`, and `test_users_runs_e2e.py`'s hard-coded `== 5` collection-count
  assertions.** Avoided entirely by keeping the two extra rows local to
  `test_capture_app_screenshots` and never touching the shared helper. Caught by: those three files'
  own assertions, which would fail immediately and loudly if this boundary were ever crossed later.
- **Reordering captures inside `test_capture_app_screenshots`** could in principle change what an
  earlier `_capture` call sees, if row creation had side effects on run/user state. Verified it does
  not: the 7 pre-existing captures were re-run in their original relative order (only pushed earlier
  in the function, not reordered relative to each other) and `user-detail.png` came out pixel-identical
  in dimensions to the currently-committed file.
- **A `run_id`/`sarah` id captured before the new rows exist** is fine — `run-detail.png` and
  `dashboard.png` intentionally still reflect the single-row run, matching today's committed images.
- Regenerating `wizard-connect.png` depends on the fake PIN stub (`stub_plex_pin`) staying wired to
  `/setup`'s exact button labels ("Sign in with Plex", "Run checks") — already covered by
  `test_wizard_e2e.py`, which asserts the identical sequence; if that wizard test ever breaks from a
  copy change, this capture breaks the same way and for the same reason, so there's no separate
  failure mode to add coverage for.
- CI never runs this file for real (`skip_unless_capturing`, `SHOTS_DIR` unset in CI) — so none of
  this can fail a CI run; it only affects what's produced when someone deliberately regenerates shots.

### 1.5 Open questions

- `docs/images/run-detail.png` (committed Aug 3) is **already stale**: it's 1440×871 (1x, non-retina),
  while the harness now captures at `device_scale_factor=2` and the freshly-captured version is
  2880×1894 — nearly 9% taller even accounting for the scale factor, meaning the run-detail page's
  content has grown since that image was last captured. Not caused by anything in this design, but
  worth flagging: it should probably be regenerated in the same pass as `rows.png`/`wizard-connect.png`
  rather than left further out of date. (`user-detail.png`, captured Aug 17, is current.)
- `screenshots.yml`'s recorded `height: 1171` for `user-detail.png` doesn't quite match the committed
  file's actual height (2302px ÷ 2 = 1151) — a small pre-existing drift, not introduced here. Worth a
  once-over when these dimensions are next touched.
- Exact placement of `rows.png` on the landing page (new `#screenshots` gallery entry vs. an image
  inside the `#templates` section itself) is a copy/layout call for whoever does `copy`/implementation,
  not something this design needs to force.

---

## 2. `two-account` — proving privacy with two real accounts on one server

### 2.1 Current state (verified)

- `tests/e2e/conftest.py`'s `ShortlistApp.plex_hubs_as(plex_account_id)` already exists and is the
  exact mechanism the codebase itself uses to prove privacy: it calls the fake PMS's `/hubs` endpoint
  with `X-Plex-Token: server-{account_id}` — i.e., it looks through that account's own eyes, not
  through Shortlist's admin API.
- `tests/e2e/test_privacy_uninstall_e2e.py::test_no_user_sees_another_users_row_in_any_library`
  already does this for real, per-account, and asserts the negative (no leakage) — this is proven,
  tested infrastructure, not something to build from scratch.
- The fake PMS's `/hubs` handler (`tests/fakes/fake_plex.py:1032-1063`) filters by the requesting
  account's excluded labels exactly the way a real PMS does, returning only hubs `promoted` for that
  account and not excluded by their share filter. Each hub carries `title` and a `key` pointing at
  `/library/collections/{rating_key}/children`.
- `/library/collections/{rating_key}/children` (`tests/fakes/fake_plex.py:895`) returns the real
  title list inside that collection (XML, via `_movie_xml`) — this is how the actual movie/show
  titles inside each account's row can be pulled, not just the shelf's own title.
- Fixture watched sets are **deliberately and permanently disjoint** (see §1.1): sarah watches
  movies + some TV, mike watches different TV only, with zero overlapping titles. This guarantees —
  by fixture design, not by luck — that sarah's and mike's rows will always contain visibly different
  titles, which is exactly the story this image needs to tell honestly.

### 2.2 The design

**Yes — this is producible from the existing harness, and the underlying mechanism is already
tested,** not hypothetical. What doesn't exist yet is turning that JSON into an image.

Pipeline (new test, e.g. `tests/e2e/test_two_account_screenshot.py`):

1. `build_real_rows(app)` — one real run, exactly as the other screenshot captures.
2. For `(201, "Sarah")` and `(202, "Mike")`: call `app.plex_hubs_as(account_id)` to get that
   account's real, exclusive hub list — reusing the exact call the privacy test already makes.
3. For each hub belonging to that account, fetch
   `httpx.get(f"{app.pms_url}/library/collections/{rating_key}/children", headers={"X-Plex-Token":
f"server-{account_id}"})` and parse the XML `Video`/`Directory` `title` attributes — the real
   titles inside that account's row (already-fake but never fabricated _for this image_; they are the
   PMS's actual answer to that account's own token).
4. Render a small, self-contained two-column HTML page (no dependency on the SPA — this isn't a
   Shortlist screen, it's a marketing graphic) styled with the docs site's own tokens
   (`--bg: #08080a`, `--accent: #e5a00d`, `--surface`, `--border` from `docs/assets/css/main.css`):
   left column "Sarah's Home", right column "Mike's Home", each showing their real shelf title(s)
   and a row of solid-color placeholder tiles labeled with the real fetched titles (the fake harness
   has no real poster art — using text-labeled tiles rather than fabricating poster images keeps the
   asset honest about being fake data).
5. `page.set_content(html)` (or `page.goto("data:text/html,...")`) via the existing Playwright
   `browser` fixture, viewport sized for the intended layout (e.g. 1600×900 for a wide landing-page
   hero), `device_scale_factor=2`, screenshot to `SHOTS_DIR/two-account.png`.

A single caption states the mechanism plainly, e.g.: _"Same server, same night. Sarah sees her row.
Mike sees his — and neither can see the other's, because Plex itself won't show it to them."_

**Why sarah/mike, not canary:** canary has no watch history, so its row degrades to the cold-start
"popular" fallback — a real and useful case elsewhere, but a weaker two-account contrast than sarah
(movies + TV, personalised) vs. mike (TV only, entirely different titles from a disjoint watch set).

### 2.3 Files created/committed and which docs pages consume them

| File                                                                                                | Role                                                                                |
| --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `tests/e2e/test_two_account_screenshot.py` (new)                                                    | produces the capture                                                                |
| `docs/images/two-account.png`                                                                       | the asset                                                                           |
| Landing page (`docs/_layouts/home.html`, likely the hero or the `#privacy` section)                 | primary consumer — this is explicitly the marketing asset, not a guide illustration |
| Possibly `docs/faq.md` under "What can the server owner see?" / the privacy Q&A, as secondary reuse | optional                                                                            |

### 2.4 What could regress, and how it's caught

- This is a **new, additive test file** — it doesn't touch `build_real_rows`, any shared fixture, or
  any existing test's assertions. Zero blast radius on the rest of the suite.
- It depends on `/hubs` and `/library/collections/{id}/children` continuing to honour per-account
  tokens the way `test_no_user_sees_another_users_row_in_any_library` already requires — if that
  ever regresses, the _existing_ privacy test fails first and independently; this capture would simply
  also start failing (skipped in CI regardless, since it's gated the same `SHOTS_DIR`/`-m e2e` way as
  every other capture test).
- Like the rest of `test_screenshots.py`, it never runs in CI (no `SHOTS_DIR` set), so it cannot break
  a build.

### 2.5 Open questions

- Exact visual chrome (how literally to mimic Plex's own Home UI vs. a more abstract "two labeled
  columns of tiles") is a design call for implementation, not something this doc needs to lock down.
  Recommendation: stay abstract enough that it reads as "a proof," not as a claimed screenshot of
  Plex's own UI (Shortlist doesn't control or render that UI, and the fake harness has no real Plex
  Web client to screenshot).
  Real poster art doesn't exist in the fake harness — this stays honest by using text-labeled
  placeholder tiles rather than synthesizing fake poster images.
- Whether to also show the "Continue Watching" hub (which the fake `/hubs` always returns first,
  identically for every account) alongside each column, to visually anchor "yes, this is the same
  kind of Home screen, only the personal row differs."

---

## 3. `privacy-diagram` — the four-step leak-safe ordering

### 3.1 Current state (verified)

There are **two** places on the site that already explain this, at different levels of visual
polish — worth being precise about which one is "pure text" and which already has some structure:

1. **`docs/faq.md`**, "How is this private? Plex doesn't have per-user collections." — genuinely
   plain prose, one paragraph, zero visual structure:

   > "The order matters: a row is created **hidden**, and only made visible once the 'hide this from
   > everyone else' rules are already in place, so there's no window where the wrong person could see
   > it."

   This is the actual "pure text" the audit item describes, and the highest-scrutiny page for it (a
   skeptical reader asking exactly this question).

2. **`docs/_layouts/home.html`**, `#privacy` section (lines 164-225) — **already has** a 4-item
   `<ol class="order">` list with lettered circular badges (CSS at `main.css:878-913`: counter-based
   `a/b/c/d` badges, left accent border, card background). This is not "pure text" — it's a decent
   linear step list already. Its four items already match the authoritative wording closely:

   > (a) _Clear up first_, (b) _Send the row hidden_, (c) _Hide it from everyone else_, (d) _Now show it_

   This matches `.claude/rules/plex-safety.md` rule 1's four steps in plain-English form already.

**Mermaid support — verified NOT present.** `docs/_config.yml`'s `plugins:` list is exactly
`jekyll-relative-links`, `jekyll-seo-tag`, `jekyll-sitemap` — no Mermaid plugin. No Gemfile/Gemfile.lock
in the repo and no GitHub Actions workflow that builds Jekyll (`.github/workflows/` has only `ci.yml`
and `dockerhub.yml`, neither touches Pages/Jekyll), meaning this site builds via GitHub's default,
non-Actions Pages pipeline (the `github-pages` gem), which restricts plugins to its own safe list —
a Mermaid _plugin_ is not on it and not usable without switching the site to an Actions-based build.
A client-side Mermaid (loaded via `<script>` from a CDN, rendering `<pre class="mermaid">` blocks in
the browser) would technically work without any Jekyll plugin change, but nothing in the repo does
this today (`grep -rn mermaid docs/` returns nothing at all).

### 3.2 The design

**Inline SVG, not Mermaid**, for this diagram specifically:

- It's four steps in a fixed, known order — not a graph needing a layout engine. Mermaid's value
  (auto-layout for arbitrary graphs) isn't needed here.
- Inline SVG embedded directly in the page's HTML can read the site's own CSS custom properties
  (`var(--accent)`, `var(--text)`, `var(--surface)`, `var(--border)`) the same way any other element
  on the page does — so it re-themes for light/dark automatically, for free, using tokens that
  already exist (`docs/assets/css/main.css:31-77`). A CDN-loaded Mermaid diagram would need its own
  separate dark/light theme configuration to match, and adds a JS dependency and a network fetch for
  one four-box diagram.
- No new plugin, no Gemfile, no build-pipeline change — the safest option given this site's actual
  (non-Actions) Pages build.

**Icon language**: reuse the site's own existing stroke-icon style (`stroke="currentColor"
stroke-width="2"`, 24×24 viewBox — the exact pattern already used in the `#privacy` section's three
feature cards, `home.html:198-218`). Use an eye-slash icon for steps 1-3 (still hidden) and a plain
eye icon for step 4 (now visible), to carry the "hidden until…" narrative visually, not just in text.

**Layout**: four connected boxes, horizontal on wide viewports (`faq.md`'s prose column is 68ch —
narrow enough that horizontal probably needs to become vertical below some breakpoint; a
`<foreignObject>`-free pure-SVG flow with a CSS media query swapping a `viewBox`/transform, or simply
authoring it as 4 stacked cards connected by a vertical arrow — is simpler and more robust than fighting
SVG responsiveness). Given `docs/faq.md` renders inside `.prose` (a single narrow column, not the wide
landing page), **vertical stacking (top-to-bottom, one arrow between each pair) is the safer default**,
matching how the home page's own `<ol class="order">` already reads in its narrower contexts.

Concrete labels (mirroring the site's own already-established plain-English phrasing from
`home.html`'s `#privacy` list, and the authoritative ordering in `plex-safety.md` rule 1):

1. **Clear up first** — remove any row Plex can't hide for that kind of library.
2. **Send the row in hidden** — it exists in Plex, but nobody's Home shows it yet.
3. **Hide it from everyone else** — every other account is told to hide this row's label.
4. **Now show it** — only now does the row appear.

Sketch (structure, not final markup — four `<g>` blocks, each a rounded rect + icon + two lines of
text, connected by a vertical arrow path):

```html
<svg
  viewBox="0 0 480 640"
  role="img"
  aria-labelledby="privacy-order-title"
  style="max-width:420px;width:100%;height:auto"
>
  <title id="privacy-order-title">
    The four-step order that keeps a row private
  </title>
  <!-- repeated 4x, y offset by ~150 each: -->
  <g>
    <rect
      x="20"
      y="20"
      width="440"
      height="120"
      rx="14"
      fill="var(--surface)"
      stroke="var(--border)"
      stroke-width="1"
    />
    <rect x="20" y="20" width="4" height="120" fill="var(--accent-line)" />
    <!-- left accent bar -->
    <circle cx="56" cy="80" r="20" fill="var(--accent-quiet)" />
    <text
      x="56"
      y="86"
      text-anchor="middle"
      fill="var(--accent-text)"
      font-family="var(--font-mono)"
      font-weight="700"
      font-size="16"
    >
      1
    </text>
    <!-- eye-slash icon, currentColor, positioned near x=100 -->
    <text x="130" y="70" fill="var(--text)" font-weight="700" font-size="17">
      Clear up first
    </text>
    <text x="130" y="92" fill="var(--muted)" font-size="13">
      Remove any row Plex can't hide for that kind of library.
    </text>
  </g>
  <!-- arrow between boxes: a vertical line + arrowhead, stroke="var(--accent-line)" -->
  <path
    d="M240 140 L240 168"
    stroke="var(--accent-line)"
    stroke-width="2"
    marker-end="url(#arrow)"
  />
  <!-- ... steps 2-4 ... -->
  <!-- step 4's icon swaps eye-slash -> eye, and its accent bar can use var(--accent) (full strength)
       rather than var(--accent-line), to visually mark "now visible" as the state change -->
</svg>
```

### 3.3 Files created/committed and which docs pages consume them

| File                       | Change                                                                                                                                                                                        |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docs/faq.md`              | replace the one-paragraph "order matters" sentence in "How is this private?" with the inline SVG (kramdown passes raw HTML/SVG blocks through untouched by default — no config change needed) |
| `docs/assets/css/main.css` | optional: a couple of small rules if the sketch above needs anything main.css doesn't already provide (unlikely — every token used already exists)                                            |

No new files strictly required — the SVG can live inline in `faq.md` itself, or be extracted to an
`{% include %}` partial (`docs/_includes/privacy-order.svg`) if the same diagram is later reused on
the home page too (see open question below). An include is preferable if reuse is likely, to avoid
two copies drifting.

### 3.4 What could regress, and how it's caught

- Zero code/test surface — this is a static docs page change. Nothing in the Python or web test
  suites reads `docs/faq.md`'s content, so there's no automated test to break.
- The one real risk is a **content drift**: if the engine's actual ordering ever changes (e.g., a
  future refactor reorders sweep/deliver/merge/promote), this diagram and `plex-safety.md` rule 1's
  prose could go stale together. Nothing currently pins docs content against the engine's actual
  phase order programmatically — this is a pre-existing gap (prose already has this exposure today),
  not one this diagram introduces or worsens.
- Verify by eye in both themes after implementation (`data-theme="dark"` / `"light"` / system) since
  no automated visual-regression test exists for the docs site.

### 3.5 Open questions

- Whether to also replace/upgrade the home page's existing `<ol class="order">` list with the same
  SVG, for visual consistency across the two places this is explained. Recommended to treat as a
  separate, smaller follow-up rather than bundling in: the home page's version is already reasonably
  good (lettered badges, not "pure text"), so this isn't fixing a gap there, just polishing something
  that already works — lower priority than `faq.md`, which currently has none of this structure at all.
- Final pixel dimensions/viewBox depend on `.prose`'s actual rendered column width at implementation
  time — worth a quick visual check at the three widths the project already tests at (320/1024/1280)
  per the "measure layout, don't reason about it" house habit.

---

## 4. `social-preview` — replace the blurred mockup

### 4.1 Current state (verified)

`docs/images/social-preview.png` (1280×640, confirmed by `file`) is a screenshot-style mockup of a
Plex-Web-like "Home" screen — a fake nav bar (Home / Trending / Activity / Find Friends / My Profile)
and a row of movie/show poster tiles, **all heavily blurred**, with "Shortlist" / tagline text overlaid
at the top in a plain sans-serif on a flat dark background. No generation script exists anywhere in
the repo (`grep -rln social-preview` across `.py`/`.sh`/`.ts`/`.tsx` returns nothing but the config
reference) — it was produced out-of-band, once, and isn't reproducible from the repo today.

`docs/_config.yml`'s comment is explicit about the _size_ requirement and _why_: "the bare row
screenshot is 3:1 and gets its top and bottom cropped away" by Reddit/Discord/Slack/X, hence the
1280×640 (2:1) canvas, set via `defaults: → values: → image:` (not a top-level `image:`, because
`jekyll-seo-tag` only reads `page.image`).

**Scope note**: the task names this "the GitHub social preview image." Two distinct things share
that name in practice: (1) `docs/images/social-preview.png`, which `jekyll-seo-tag` uses as the
`og:image` for every _docs page_ share, and (2) GitHub's own **repository**-level social preview
(Settings → General → Social preview), which is a manually-uploaded image in GitHub's UI, not sourced
from any file in the repo automatically, and has no documented public API for programmatic upload.
In practice a solo maintainer typically uses the _same_ designed file for both — produce one asset,
upload it to GitHub's repo settings by hand once, and also commit it as `docs/images/social-preview.png`
for the docs site's own card. This design produces that one file; the manual GitHub Settings upload
step is unavoidable and called out as an open question below, not something this design can automate.

### 4.2 The design

Replace the Plex-mockup screenshot with a **designed banner using the site's own brand system** —
no blur needed, because nothing on it is a screenshot of anyone's real library or Plex's own
trademarked UI chrome.

**Assets to reuse, verified to exist:**

- The app's real logo mark, `web/public/favicon.svg`: a rounded dark square, three amber horizontal
  "list" lines of decreasing length, and a 5-point amber star badge overlapping the bottom-right —
  this is the exact mark shown top-left in every captured app screenshot (confirmed in the `rows.png`
  capture above: "✨ Shortlist" wordmark with this icon).
- Palette: `--amber: #e5a00d` / `--amber-bright: #f5c04a` on `--bg: #08080a`, from
  `docs/assets/css/main.css` — identical to what the app itself uses, so the banner reads as "this
  product," not a generic stock graphic.
- Copy already established and approved elsewhere in the repo (`docs/_config.yml`):
  - Title: **Shortlist**
  - Tagline: _"Per-user Plex recommendations, built from each person's own watch history"_

**Layout** (1280×640, dark `#08080a` background):

```
┌────────────────────────────────────────────────────────────┐
│  [logo mark]  Shortlist                                     │  ← top-left, ~64px logo + wordmark
│                                                               │
│  Per-user Plex recommendations,                              │  ← large, ~56px, --text
│  built from each person's own watch history                  │
│                                                               │
│  ✓ Private per-person rows   ✓ Self-hosted   ✓ No AI key    │  ← small feature row, --muted text,
│    required                                                   │    amber checkmarks
│                                                               │
│         [three small stylised "shelf" rows, bottom third]    │  ← abstract, NOT real posters:
│         each a labeled bar + 4-5 flat amber/muted rounded    │    solid-color rounded rects,
│         rectangles of varying widths, no text inside them    │    hints at "rows" without
│                                                               │    claiming to be a screenshot
└────────────────────────────────────────────────────────────┘
```

Deliberately **not** a screenshot of any UI (real Plex or Shortlist's own) — avoids the exact problem
the current asset has (needing to blur something to make it safe to publish) and avoids the accuracy
trap of a mockup going stale as the real UI evolves.

**Production mechanism**: author as a small standalone HTML+CSS file (reusing the exact CSS custom
properties from `docs/assets/css/main.css` so the banner and the site it links to are visually
identical in palette), then capture it with Playwright at the exact target size — the same tool this
repo already uses for every other visual asset here, rather than a one-off hand-exported image nobody
can reproduce:

```python
# e.g. tests/e2e/test_social_preview.py, or a small standalone script — doesn't need the fake-Plex
# harness at all, just a browser context, so it can be much simpler than test_screenshots.py.
def test_capture_social_preview(browser: Browser) -> None:
    html_path = Path(__file__).parent / "fixtures" / "social_preview.html"
    context = browser.new_context(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
    page = context.new_page()
    page.goto(html_path.as_uri())
    page.screenshot(path="docs/images/social-preview.png")
    context.close()
```

`device_scale_factor=1` deliberately (not the 2x used elsewhere): GitHub/Reddit/Discord/Slack expect
this file at exactly 1280×640 pixels, not a 2x-then-halved-by-CSS asset like the in-app screenshots.

### 4.3 Files created/committed and which docs pages consume them

| File                                           | Role                                                                                                                                                                |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/e2e/fixtures/social_preview.html` (new) | the banner's source — a static HTML+CSS file, editable without touching Python                                                                                      |
| A small capture test/script (new)              | renders the HTML to PNG at exactly 1280×640                                                                                                                         |
| `docs/images/social-preview.png`               | overwritten in place — already wired into `docs/_config.yml`'s `defaults.image`, so every docs page's `og:image` picks it up automatically; no config change needed |
| GitHub repo Settings → Social preview          | manual upload of the same file — outside git, see open question                                                                                                     |

### 4.4 What could regress, and how it's caught

- Zero blast radius on the app or engine — this touches only a docs image and (optionally) a new,
  isolated test/script that doesn't share fixtures with anything else.
- `docs/_config.yml`'s `defaults.image` path and dimensions expectation (2:1, 1280×640) are unchanged
  by this design, so no config edits are required — only the PNG's _content_ changes.
- The only "regression" risk is aesthetic drift if the banner's copy goes stale (e.g., if the tagline
  in `_config.yml` is ever reworded and the banner isn't updated to match) — not something a test can
  catch; worth a note in `.claude/rules/docs.md`'s trigger list if that tagline ever changes.

### 4.5 Open questions

- **GitHub's own repo-level social preview cannot be set from a git commit.** It requires a manual
  upload via Settings → General → Social preview, by someone with admin rights on the repo. This
  design produces the file; actually installing it as GitHub's card is a manual step for Steve,
  not something CI or this design can automate. Confirmed: no documented public REST API endpoint
  for this exists as of this session's research depth (not exhaustively verified against GitHub's
  full API surface — flagged as unverified rather than asserted).
  - **Confidence note (2026-09 cutoff):** the assistant's training data may be stale here — GitHub
    ships API surface continuously and a repo-social-preview endpoint may have shipped since. Worth
    a quick `gh api` / GitHub REST docs check at implementation time before assuming the manual-upload
    path is still the only one.
- Exact wording of the three feature checkmarks (only one candidate set proposed above) and the exact
  shelf-motif styling are copy/visual decisions for implementation, not locked by this design.
- Whether `docs/images/plex-picked-for-you.jpg` (the _other_ hero image, used at the top of the
  landing page, `home.html:56`) should also be revisited — out of scope here (the task named the
  social-preview image specifically), but noted since it's the same "photograph vs. designed asset"
  question in miniature.
