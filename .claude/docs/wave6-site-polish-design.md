# Wave 6 — site polish design (copy, badges, motion, seo)

From `.claude/docs/audit-2026-09-programme.md`. Covers four independent items on the public docs
site (`docs/`, Jekyll, GitHub Pages project site at `/shortlist/`) and `README.md`. Every claim
below was checked against the live files or the live site/API — see "Files and URLs checked" at
the end. Nothing here contradicts the SEO work already done this month (sitemap submission,
`jekyll-seo-tag`/`jekyll-sitemap`, the `superpowers/` exclude); item `seo`(a) below explicitly
narrows the scope of a premise in the brief rather than extending it.

---

## 1. `copy` — six home-page copy fixes

Read `docs/index.md` (front matter only — `layout: home`) and the real content in
`docs/_layouts/home.html`, plus the data files it loops over (`_data/features.yml`,
`_data/screenshots.yml`, `_data/works_with.yml`, `_data/templates.yml`). The page has already been
through real editing — its own header comment says an earlier draft had 29 em dashes and read like
a machine wrote it, and the current copy mostly holds up. The six weakest spots below are real
regressions against that same house style, each verified in the file, not invented to hit a count
of six.

### 1. Hero bullet is vague out of context — `docs/_layouts/home.html:48`

**Current:**

```html
Puts your Plex back if you remove it
```

This is the third of three hero bullets, read before anyone has seen the privacy section that
explains what "puts back" means. Taken alone it could mean "restores your Plex Media Server," which
isn't what happens. It's also the only one of the three bullets that isn't a tight noun phrase
(`One Docker container`, `No AI key required`) — it's noticeably longer and reads as an afterthought.

**Fix:**

```html
Undoes itself if you remove it
```

Matches the plain, concrete promise the FAQ and the privacy section already make ("Your server ends
up as we found it") without requiring the reader to have read them first.

### 2. Stray em dash in the hero figcaption — `docs/_layouts/home.html:59`

**Current:**

```html
A &ldquo;Picked for You&rdquo; row on Plex &mdash; private to that user, and
built from what they watched.
```

`home.html`'s own top-of-file comment (lines 4-10) states the house rule: "almost no em dashes... An
earlier version had 29 of them." This is the one line on the page that still has one — confirmed by
`grep -n "—\|&mdash;" docs/_layouts/home.html`, which returns exactly this line and nothing else.

**Fix:**

```html
A &ldquo;Picked for You&rdquo; row on Plex, private to that user and built from
what they watched.
```

### 3. Unexplained jargon in the problem statement — `docs/_layouts/home.html:74`

**Current** (full paragraph, lines 72-76):

```html
<p class="section__lede">
  Everyone on your server opens Plex to a big library and no idea what to put
  on. The rows Plex builds are the same for every account and lean on its own
  streaming catalogue, so they don't know your sister has watched every heist
  film you own and your dad hasn't touched one.
</p>
```

"lean on its own streaming catalogue" is accurate (Plex's Recommended/Home rows do pull in titles
from Plex's own ad-supported streaming catalogue, not just your library) but it's unexplained
jargon to a reader who doesn't already know that — the one vague phrase in an otherwise concrete
paragraph.

**Fix:**

```html
<p class="section__lede">
  Everyone on your server opens Plex to a big library and no idea what to put
  on. The rows Plex builds are the same for every account and often suggest
  what's in Plex's own streaming catalogue rather than what's in your library,
  so they don't know your sister has watched every heist film you own and your
  dad hasn't touched one.
</p>
```

### 4. Repetitive, unclear heading — `docs/_layouts/home.html:252`

**Current:**

```html
<h2>See why each person got what they got</h2>
```

"got what they got" reads as a stutter, not a stylistic choice — the only heading on the page with
this problem.

**Fix:**

```html
<h2>See the reasoning behind each row</h2>
```

### 5. Vague call-to-action in the dry-run callout — `docs/_layouts/home.html:382-387`

**Current:**

```html
<div class="callout mt-lg">
  ...
  <div>
    <p>
      <strong>Want to try it without touching your server?</strong> Add
      <code>-e SHORTLIST_DRY_RUN=1</code>. Shortlist will show you everything it
      would do and write nothing to Plex until you are happy.
    </p>
  </div>
</div>
```

"until you are happy" doesn't say what to actually do — it violates `.claude/rules/frontend.md`'s
"controls say exactly what happens" (this is copy, not a control, but the docs house style applies
the same standard, per the file's own comment block).

**Fix:**

```html
<p>
  <strong>Want to try it without touching your server?</strong> Add
  <code>-e SHORTLIST_DRY_RUN=1</code>. Shortlist will show you everything it
  would do and write nothing to Plex. Remove the flag when you're ready for it
  to make real changes.
</p>
```

### 6. "Needed" vs. "required" — `docs/_layouts/home.html:44`

**Current:**

```html
No AI key needed
```

Every other instance of this claim on the site uses "required": `docs/index.md:8` ("no AI key
required"), `docs/_config.yml:7` (site-wide description, same wording), and `README.md`. This hero
bullet is the sole outlier — confirmed by
`grep -rn "AI key" docs/_layouts/home.html docs/index.md docs/_config.yml`.

**Fix:**

```html
No AI key required
```

### How this is verified

Build the site locally (`cd docs && bundle exec jekyll serve`, or preview via the existing GitHub
Pages deploy on the next `dev` push) and read the six changed sections at rest. Confirm zero em
dashes remain: `grep -n "—\|&mdash;" docs/_layouts/home.html` should return nothing. Confirm the
"required"/"needed" split is gone: `grep -rn "AI key" docs/` should show "required" everywhere.

### What could regress

None of these six touch a link's `href`, a data file consumed elsewhere (`_data/*.yml` are
untouched), or anything Jekyll-processed beyond plain text — pure copy edits in one layout file.
The only risk is taste: fix 4's new heading ("See the reasoning behind each row") covers the
_picks_ screenshot well but the section also shows a _run detail_ screenshot, which is about
process transparency more than "reasoning" — the eyebrow directly above it ("Behind the row")
already covers that half, so the heading doesn't need to.

### Open questions

None — all six are verified against the current file content, not assumptions.

---

## 2. `badges` — 8 badges in shields.io default colours

`grep -rln "shields.io" --include="*.md" --include="*.html" --include="*.yml"` across the repo
returns only `README.md` — the docs site itself carries no badges. Fetched every badge's live SVG
(`curl` each URL, `grep -o 'fill="#[0-9A-Za-z]*"'`) rather than trusting the query string, since a
shields.io badge's rendered colour depends on live upstream data, not just the URL.

### Verified current state

| Badge        | File:line       | Rendered colour (verified)           | Static or dynamic?                                                                                                                |
| ------------ | --------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| build        | `README.md:247` | `#4b0` (bright green = passing)      | **Dynamic** — flips red on a failing `master` build                                                                               |
| release      | `README.md:249` | `#007ec6` (shields "blue", fixed)    | Static                                                                                                                            |
| codecov      | `README.md:251` | `#67ac09` (green)                    | **Dynamic** — scales with coverage % (verified: `psf/requests` at 67% renders `#d8b800` yellow, a repo with no data renders grey) |
| docker pulls | `README.md:253` | `#066da5` (Docker's own blue, fixed) | Static — verified identical on `library/nginx` (huge pull count)                                                                  |
| image size   | `README.md:255` | `#007ec6` (blue, fixed)              | Static — verified identical on `alpine` vs `ubuntu`                                                                               |
| stars        | `README.md:257` | `#007ec6` (blue, fixed)              | Static — verified identical on `facebook/react` (230k+ stars)                                                                     |
| issues       | `README.md:259` | `#4b0` (green, 0 open issues)        | **Dynamic** — verified: `facebook/react` (1,342 open) renders `#d8b800` yellow                                                    |
| license      | `README.md:261` | `#67ac09` (green)                    | Static per-repo (won't change unless the license file changes)                                                                    |

`ai-shield` (`README.md:263`) and `sponsor-shield` (`README.md:265`) already carry explicit brand
colours (`8A2BE2` purple for Anthropic, `db61a2` pink for GitHub Sponsors) and are correctly
excluded from the count of 8 — they're deliberately off-amber, not defaulted.

The project's amber is `--amber: #e5a00d` (`docs/assets/css/main.css:16`, "Plex's own #e5a00d").

**The premise ("8 badges in default colours") is true, verified two ways: URL inspection (none of
the 8 carries a `color=`/`labelColor=` param) and rendered-pixel inspection (the actual `fill`
values above).** But three of the eight — build, codecov, issues — are genuine status indicators
whose colour changes to signal something (build passing/failing; coverage and open-issue count on
a scale). Flattening all eight to a single static amber would erase that signal on those three,
which is a real, unstated cost the brief's framing doesn't account for.

### The fix

**Static badges (5) — full amber, safe to recolour completely:**

```
[release-shield]: https://img.shields.io/github/v/release/stevezau/shortlist?style=for-the-badge&label=release&color=e5a00d
[docker-shield]: https://img.shields.io/docker/pulls/stevezzau/shortlist?style=for-the-badge&color=e5a00d
[size-shield]: https://img.shields.io/docker/image-size/stevezzau/shortlist/latest?style=for-the-badge&label=image&color=e5a00d
[stars-shield]: https://img.shields.io/github/stars/stevezau/shortlist.svg?style=for-the-badge&color=e5a00d
[license-shield]: https://img.shields.io/github/license/stevezau/shortlist.svg?style=for-the-badge&color=e5a00d
```

**Status badges (3) — recolour only the label chip (`labelColor`), leave the status-driven message
colour alone:**

```
[build-shield]: https://img.shields.io/github/actions/workflow/status/stevezau/shortlist/ci.yml?branch=master&style=for-the-badge&label=build&labelColor=e5a00d
[codecov-shield]: https://img.shields.io/codecov/c/github/stevezau/shortlist?style=for-the-badge&labelColor=e5a00d
[issues-shield]: https://img.shields.io/github/issues/stevezau/shortlist.svg?style=for-the-badge&labelColor=e5a00d
```

This still brings all 8 into the amber scheme (every badge now shows amber somewhere) without
silencing "the build is red" or "coverage dropped."

### How this is verified

Re-fetch each new URL and re-run the same `fill="#..."` grep used above: the 5 static badges should
show `fill="#e5a00d"` on the message (right) segment; the 3 status badges should show it on the
label (left) segment while the message segment still varies (spot-check by comparing against a
repo known to have a different build/coverage/issue state, as done above).

### What could regress

**Contrast.** shields.io doesn't expose a text-colour parameter — badge text is fixed white
(confirmed: every fetched SVG above has `fill="#fff"` text) regardless of background. `#e5a00d`
against white text computes to a WCAG contrast ratio of **~2.24:1** — below even the 3:1 floor for
UI components. This is not a hypothetical: this exact colour is why `.btn--primary`
(`docs/assets/css/main.css:392-394`) deliberately pairs `--amber` with near-black text
(`color: #1a1200`), not white — the codebase already made this call once. `--amber-deep`
(`#b87d05`) computes to **~3.5:1**, clears the UI-component floor but still falls short of 4.5:1 for
body text. Badge readability conventions across the shields.io ecosystem are looser than WCAG body
text in practice (plenty of default shields colours — e.g. `yellow`, `#dfb317` — sit in the same
range), so this may be an acceptable trade either way, but it's a real regression risk worth
deciding deliberately rather than by accident.

### Open questions

**Colour choice:** `e5a00d` (exact brand amber, ~2.24:1 contrast) or `b87d05`/`--amber-deep`
(~3.5:1, safer, still visibly "amber," matches the app's own text on this colour) — Steven's call,
not decided here.

---

## 3. `motion` — one fade-up reveal utility for the docs site

Scope: `docs/assets/css/main.css` and `docs/assets/js/site.js` only (Jekyll site) — not `web/`
(React app), which `.claude/rules/frontend.md` governs separately and which this task doesn't touch.

### Current state

`site.js` already uses one `IntersectionObserver` (line 125, for the table-of-contents scroll
highlight). `main.css` already has a `prefers-reduced-motion` block (lines 97-108):

```css
@media (prefers-reduced-motion: reduce) {
  html {
    scroll-behavior: auto;
  }
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

That's a universal selector with `!important` — it already collapses _any_ CSS transition on the
page to near-zero duration under reduced motion, including the new utility below. No new
reduced-motion media query is needed; duplicating one would just be dead code repeating a rule that
already applies.

No existing "reveal on scroll" utility exists (`grep -n "reveal\|fade-up"` in both files returns
nothing).

### The fix

**CSS** — append to the "utils" section of `docs/assets/css/main.css` (after the existing `.mt-lg`
rule, currently ending at line 1740):

```css
/* ------------------------------------------------------------- reveal -- */
/* Fade-up for section reveals. Visible at rest by design: `.reveal` alone carries
   no hidden state, so a page with this file's CSS but no JS (or JS that errors,
   or a browser with no IntersectionObserver) shows the section normally. Only
   `site.js` — once it has confirmed it can observe and un-hide elements — adds
   `.js-reveal-ready` to <html>, and that's the ONLY thing that puts a section
   into the hidden starting state. CSS never hides content on its own. */
.reveal {
  opacity: 1;
  transform: none;
}

.js-reveal-ready .reveal {
  opacity: 0;
  transform: translateY(16px);
  transition:
    opacity 0.5s var(--ease),
    transform 0.5s var(--ease);
}

.js-reveal-ready .reveal.is-visible {
  opacity: 1;
  transform: none;
}
```

(No separate `prefers-reduced-motion` block: the sitewide rule at `main.css:97-108` already forces
`transition-duration: 0.01ms !important` on every element, so `.js-reveal-ready .reveal`'s
transition is already imperceptible under reduced motion — the section still ends up visible, just
without the animated step.)

**JS** — add to `docs/assets/js/site.js`, inside the existing top-level IIFE (e.g. after the
table-of-contents block, ~line 145), reusing the `root` variable already defined at line 7:

```js
/* ------------------------------------------------------- section reveal -- */
/* Progressive enhancement only, same rule as the rest of this file (see the
   header comment): `.reveal` elements are visible by default in CSS. This block
   is the ONLY thing that can hide one, and only once it has verified it can also
   un-hide it — so a thrown error, a blocked script, or a browser with no
   IntersectionObserver leaves every section at its default visible state rather
   than stuck invisible. */
var reduceMotion =
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
var reveals = document.querySelectorAll(".reveal");
if (reveals.length && !reduceMotion && "IntersectionObserver" in window) {
  root.classList.add("js-reveal-ready");
  var revealObserver = new IntersectionObserver(
    function (entries, obs) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          obs.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.15, rootMargin: "0px 0px -10% 0px" },
  );
  reveals.forEach(function (el) {
    revealObserver.observe(el);
  });
}
```

Skipping the whole block under `prefers-reduced-motion` (rather than relying only on the CSS
override) means those users never pay for the observer at all, not just that its effect is
invisible.

**Usage** — add the class to section wrappers in `docs/_layouts/home.html`, e.g.:

```html
<section class="section reveal" id="how-it-works"></section>
```

Apply to whichever sections should reveal on scroll; every one of them already renders fully
visible with no class added, so adding it to some sections and not others is safe either way.

### How this is verified

1. With JS enabled: scroll the built page and confirm sections fade up once, not on every
   re-entry (the `obs.unobserve` call makes it one-shot).
2. With JS disabled (browser dev tools → disable JavaScript) or `site.js` renamed: reload and
   confirm every `.reveal` section is fully visible immediately — this is the check the brief
   specifically asked for.
3. With reduced motion forced (macOS: System Settings → Accessibility → Display → Reduce motion;
   or Chrome DevTools → Rendering → Emulate CSS media feature `prefers-reduced-motion: reduce`):
   confirm sections appear immediately with no visible fade or slide.
4. `pnpm` has no role here — this is the Jekyll site, not `web/`; there is no vitest/tsc coverage
   for `docs/assets`. Manual verification via a local `bundle exec jekyll serve` (or the live
   preview after a `dev` push) is the available check.

### What could regress

`will-change: opacity, transform` was deliberately left out of the CSS above — it was in an earlier
draft of this design but adds GPU-layer cost on every `.reveal` section for a one-shot animation,
which is the wrong trade for a docs site; removed rather than shipped as unnecessary. If a section
containing a `<details>` (the FAQ accordion, `home.html:407`) gets `.reveal` and a user opens it
from a search-engine deep link before it has scrolled into view, `opacity: 0` from `.js-reveal-ready`
would hide already-open content until it scrolls — worth excluding accordions from this class, or
scoping `.reveal` to section wrappers only (as designed) rather than individual interactive elements.

### Open questions

Which sections get `.reveal` is a design/taste call for whoever applies it — this design provides
the mechanism and one example, not a full page audit of which sections should animate.

---

## 4. `seo` — four items, one of which turned out not to apply as stated

### (a) FAQ structured data on `/faq/` — value is narrower than the brief assumes

**Verified finding that changes the fix:** Google discontinued the FAQ rich result. Fetching
`https://developers.google.com/search/docs/appearance/structured-data/faqpage` returns an HTTP 301
redirect to `/search/updates#removing-faq-rich-result` (checked live, 2026-09-05) — the documentation
page for `FAQPage` no longer exists; it redirects straight to Google's own changelog entry
announcing the removal. Cross-checked against Google's current structured-data gallery
(`.../structured-data/search-gallery`): `FAQPage` is absent from the ~30-item current list.
`Breadcrumb` **is** present in that same list (relevant to item (b) below).

So: adding `FAQPage` JSON-LD to `/faq/` will not produce a Google rich result — that channel is
closed, for every site, not just this one. It isn't a wasted fix, but its remaining value is
narrower: `head.html`'s own existing comment already states the real reason structured data is on
this site at all — "AI crawlers read it to work out what this software is." Schema.org-typed FAQ
content is still exactly the kind of thing an AI answer engine or another search index (unverified
whether Bing still honours it — flagged below) can quote. Treat this as a low-priority, non-Google
addition, not the SEO win the brief's framing implies.

**Current state:** `docs/_layouts/home.html:442-460` already emits `FAQPage` JSON-LD, sourced from
`docs/_data/faq.yml` (9 Q&As) — but only on the home page. `docs/faq.md` (the actual `/faq/` page,
13 more detailed Q&As, confirmed via `grep -c "^## " docs/faq.md`) carries none. The page with the
real, complete FAQ content is the one page without structured data for it.

**The fix** — reuse `site.data.faq` (the same 9-question set already used and already documented as
"must match /faq, which is the fuller version," `_data/faq.yml:6`) rather than migrating all 13
prose Q&As into a new data file. Appended to the end of `docs/faq.md`:

```liquid
{%- comment -%}
FAQPage structured data, reusing the same _data/faq.yml the home page's teaser renders (see
home.html) so this page's structured data and the teaser can never disagree. This page has more
questions than faq.yml on purpose — faq.yml is deliberately a short subset (see its own header
comment) — the reused subset is accurate, just partial.

Google retired the FAQ rich result in 2023 and removed the feature outright (see the redirect at
.../structured-data/faqpage -> /search/updates#removing-faq-rich-result, live-checked 2026-09-05).
This produces no Google rich result. It stays valid schema.org input for AI crawlers and any other
index that still reads FAQPage — the same reasoning already used for the SoftwareApplication block
in head.html.
{%- endcomment -%}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {%- for q in site.data.faq -%}
    {
      "@type": "Question",
      "name": {{ q.q | jsonify }},
      "acceptedAnswer": { "@type": "Answer", "text": {{ q.a | markdownify | strip_html | normalize_whitespace | strip | jsonify }} }
    }{%- unless forloop.last -%},{%- endunless -%}
    {%- endfor -%}
  ]
}
</script>
```

This is the identical Liquid already in `home.html:446-460`, pointed at the same data — zero drift
risk because it's the same loop over the same source, not a re-authored copy.

An alternative (migrate faq.md's full 13 Q&As into a new `_data/faq_page.yml` so the structured data
matches the complete page) was considered and rejected here: it requires re-authoring markdown-with-
formatting (bold, code, a comparison table in the web-search question) into YAML, and gains nothing
now that there's no rich result to be complete _for_. Worth revisiting only if a future consumer
(an AI crawler, a Bing feature) is confirmed to reward completeness over the teaser subset.

**Verified:** validate the emitted JSON-LD against schema.org's `FAQPage` shape with Google's Rich
Results Test (it still parses and validates schema.org types even for ones with no Search feature)
or `https://validator.schema.org/`. Confirm the page renders 9 `Question` entries.

**What could regress:** none of the 8 remaining Q&As on `/faq/` beyond that reused set gain
structured data (the web-search-backend question, which contains a markdown table, is one of the
13 not reused — deliberately, since Google's answer-text guidance for the (now-defunct) feature
stripped unsupported tags like tables, so including it would have produced a garbled plain-text
run-on of a table's cells).

**Open question:** whether Bing (or another consumer that still credits FAQPage) exists and would
reward the fuller 13-question version enough to justify the YAML migration — not verified here.

### (b) Breadcrumb structured data — real, unimplemented, adds cleanly

**Current state:** no `BreadcrumbList` anywhere (`grep -rn "BreadcrumbList\|breadcrumb" docs/`
returns nothing). `Breadcrumb` is confirmed present in Google's current structured-data gallery, so
unlike (a) this one still functions as SEO.

**The fix** — `docs/_layouts/doc.html` already computes everything a breadcrumb trail needs, for the
pager (lines 9-32): `nav`, `idx` (top-level nav position), `parent_idx`/`child_idx` (for a Guides/Plex
how-to sub-page). Reuse those exact variables so a page's breadcrumb can never name a different
parent than its own prev/next links do. Insert after that block (after line 32, before
`<div class="docs">` on line 34):

```liquid
{%- comment -%}
BreadcrumbList, built from the same nav lookup as the pager above (idx/parent_idx/child_idx) so
this can't disagree with prev/next about a page's place in the site. Emits nothing for a page not
in site.nav -- no invented trail beats a wrong one.
{%- endcomment -%}
{%- if idx >= 0 or child_idx >= 0 -%}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {
      "@type": "ListItem",
      "position": 1,
      "name": "Home",
      "item": {{ '/' | absolute_url | jsonify }}
    }
    {%- if child_idx >= 0 -%}
    ,{
      "@type": "ListItem",
      "position": 2,
      "name": {{ nav[parent_idx].title | jsonify }},
      "item": {{ nav[parent_idx].url | absolute_url | jsonify }}
    },
    {
      "@type": "ListItem",
      "position": 3,
      "name": {{ nav[parent_idx].children[child_idx].title | jsonify }},
      "item": {{ page.url | absolute_url | jsonify }}
    }
    {%- else -%}
    ,{
      "@type": "ListItem",
      "position": 2,
      "name": {{ nav[idx].title | jsonify }},
      "item": {{ page.url | absolute_url | jsonify }}
    }
    {%- endif -%}
  ]
}
</script>
{%- endif -%}
```

Every `doc`-layout page in `docs/` is registered in `site.nav` in `_config.yml` (checked: `index.md`
uses `home` layout and is excluded by design; every other `.md` file — `faq.md`, `getting-started.md`,
`reference.md`, `plex-how-to.md` and its 7 children, `guides.md` and its children — has a `nav`
entry), so the `{%- if idx >= 0 or child_idx >= 0 -%}` guard should never actually suppress output
today; it's there for the day a page is added without a nav entry, so it fails closed (no
breadcrumb) rather than emitting one with a blank `name`.

**Verified:** Google's own documented shape (fetched from
`.../structured-data/breadcrumb`) requires `position` (starting at 1), `name`, and `item` (URL,
required on every item except optionally the last) inside `itemListElement` — matched exactly
above. Validate with the Rich Results Test or `validator.schema.org` against a built page (e.g.
`/plex-per-user-collections/`, which exercises the 3-level child-page path) and `/faq/` (the
2-level top-level-page path).

**What could regress:** none — this is additive `<script>` markup with a fail-closed guard; it
changes no visible content and no existing Liquid variable.

**Open questions:** none.

### (c) Duplicate canonical tag — confirmed real, root cause identified

**Verified directly from the live site**, not just the source:

```
$ curl -s https://stevezau.github.io/shortlist/ | grep -n 'rel="canonical"'
14:<link rel="canonical" href="https://stevezau.github.io/shortlist/" />
31:<link rel="canonical" href="https://stevezau.github.io/shortlist/">
```

Same result on `/faq/` and `/404.html` — two canonical tags, same URL, on every page. Source: line
14's tag is `jekyll-seo-tag`'s own output from `{% seo %}` (`docs/_includes/head.html:4`). Line 31's
tag is a **second, manual** one at `docs/_includes/head.html:9`:

```html
<link rel="canonical" href="{{ page.url | absolute_url }}" />
```

`jekyll-seo-tag` has emitted a canonical tag by default since early versions of the plugin — the
manual line at `head.html:9` was redundant from the moment `{% seo %}` was added, and nobody has
needed to notice because the two tags happen to always agree.

**The fix** — delete the manual line. `docs/_includes/head.html:9`:

```diff
- <link rel="canonical" href="{{ page.url | absolute_url }}">
```

Nothing replaces it; `{% seo %}` already produces the equivalent tag.

**Verified:** rebuild, then `curl | grep 'rel="canonical"'` on the home page, `/faq/`, and
`/404.html` (checked above to confirm all three currently produce identical duplicate values, so
none of them depends on the manual line for a _different_ URL) — each should show exactly one tag,
at the same URL the duplicate previously agreed on.

**What could regress:** none identified. Both tags render byte-identical URLs on every page type
checked (home, a nav page, the 404 page), so there's no case observed where the manual tag was
covering for `jekyll-seo-tag` computing something different.

**Open questions:** none.

### (d) Docker Hub category/metadata — category is genuinely unset; automation isn't supported

**Verified live:** `curl -s https://hub.docker.com/v2/repositories/stevezzau/shortlist/` (public,
unauthenticated, read-only) returns `"categories": []`. Confirmed empty, not a false premise.

**Docker Hub's fixed category taxonomy has no home-media/self-hosted option.** The full list
(`curl -s https://hub.docker.com/v2/categories/`, 16 entries): Networking, Security, Languages &
frameworks, Integration & delivery, Message queues, API management, Internet of things, Machine
learning & AI, Developer tools, Data science, Web servers, Operating systems, Content management
system, Databases & storage, Monitoring & observability, Web analytics. Nothing fits "personal media
recommendation tool" cleanly. Checked how comparable images handle this:
`linuxserver/sonarr`, `linuxserver/radarr`, `plexinc/pms-docker`, and `organizr/organizr` all also
carry `"categories": []` — they didn't solve this either. `linuxserver/overseerr` (functionally the
closest precedent: a personalized-request tool that also talks to Radarr/Sonarr) is the one example
found with categories set: `["Integration & delivery", "Content management system"]` — an imperfect
but deliberate-looking fit, not a default.

**The fix — recommend the same two categories Overseerr uses**, as the closest verified precedent in
a taxonomy with no better option: `integration-and-delivery` (fits the Radarr/Sonarr/Overseerr-style
automation) and `content-management-system` (fits managing Plex collections).

**How to apply it — manually, once, not via the workflow:**

1. `hub.docker.com` → `stevezzau/shortlist` → repository settings → General → Categories → select
   "Integration & delivery" and "Content management system" → Save.

**Automating it was considered and rejected for the workflow.** `.github/workflows/dockerhub.yml`
already syncs the description via `peter-evans/dockerhub-description@v5`; its `action.yml` (fetched
from the `v5` tag) declares exactly six inputs — `username`, `password`, `repository`,
`short-description`, `readme-filepath`, `enable-url-completion`, `image-extensions` — **no
`categories` input exists.** A search of Docker's published Hub API reference
(`docs.docker.com/reference/api/hub/latest/`) found no documented, versioned endpoint for writing
`categories` either (the read-only `.../v2/categories/` list used above is the only category-related
endpoint confirmed to exist and work). Setting it via API would mean calling the same undocumented
endpoint the Hub web UI itself uses, authenticated with a `hub.docker.com` session/JWT
(`/v2/users/login/`) rather than the container-registry PAT already stored as `DOCKERHUB_TOKEN` —
a second, unversioned credential and an unofficial contract, for a value that's set once and rarely
changes. That's a worse trade than the description sync (which runs on every relevant push and does
have a versioned Action backing it). Recommend the one-time manual edit above instead.

**Verified:** re-`curl` `https://hub.docker.com/v2/repositories/stevezzau/shortlist/` after the
manual change and confirm `"categories"` is no longer `[]`.

**What could regress:** nothing — this is a metadata field on Docker Hub's own page, unrelated to
image builds, tags, or the description-sync workflow.

**Open questions:** whether "Integration & delivery" / "Content management system" is actually the
best available pair is a judgment call within a taxonomy that has no correct answer — flagged, not
resolved, here.

---

## Files and URLs checked

**Read:** `.claude/CLAUDE.md`, `.claude/rules/docs.md`, `.claude/rules/frontend.md`,
`.claude/docs/orphan-guard-design.md`, `docs/_config.yml`, `docs/index.md`,
`docs/_layouts/home.html`, `docs/_layouts/doc.html`, `docs/_layouts/default.html`,
`docs/_includes/head.html`, `docs/faq.md`, `docs/_data/faq.yml`, `docs/_data/features.yml`,
`docs/_data/screenshots.yml`, `docs/_data/works_with.yml`, `docs/_data/templates.yml`,
`docs/assets/css/main.css`, `docs/assets/js/site.js`, `README.md`,
`.github/workflows/dockerhub.yml`, `.github/dockerhub-overview.md`.

**Live-fetched for verification:** `https://stevezau.github.io/shortlist/`,
`.../faq/`, `.../404.html`, `.../nonexistent-test-page-xyz/` (canonical-tag check);
`https://developers.google.com/search/docs/appearance/structured-data/faqpage` (301 redirect check),
`.../structured-data/breadcrumb`, `.../structured-data/search-gallery`; 8 README badge SVGs plus
comparison badges on `torvalds/linux`, `facebook/react`, `library/nginx`, `library/alpine`,
`library/ubuntu`, `psf/requests` (colour-dynamism checks); `https://hub.docker.com/v2/categories/`
and `.../v2/repositories/{stevezzau/shortlist, linuxserver/sonarr, linuxserver/radarr,
plexinc/pms-docker, organizr/organizr, linuxserver/overseerr}/` (Docker Hub category checks);
`peter-evans/dockerhub-description`'s `action.yml` at the `v5` tag; `docs.docker.com/reference/api/hub/latest/`.
