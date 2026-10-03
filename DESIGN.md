---
name: Shortlist
description: The self-hosted admin console for private, per-person Plex recommendation rows.
colors:
  primary: "hsl(43 92% 55%)"
  primary-foreground: "hsl(30 90% 8%)"
  accent: "hsl(43 55% 15%)"
  accent-foreground: "hsl(43 92% 72%)"
  background: "hsl(240 8% 6%)"
  card: "hsl(240 6% 9%)"
  elevated: "hsl(240 6% 12%)"
  raised: "hsl(240 6% 15%)"
  secondary: "hsl(240 5% 15%)"
  muted: "hsl(240 5% 13%)"
  border: "hsl(240 5% 17%)"
  border-strong: "hsl(240 5% 24%)"
  input: "hsl(240 5% 19%)"
  foreground: "hsl(40 12% 94%)"
  muted-foreground: "hsl(40 5% 64%)"
  faint-foreground: "hsl(40 4% 52%)"
  success: "hsl(152 55% 45%)"
  warning: "hsl(38 92% 52%)"
  destructive: "hsl(0 72% 51%)"
  destructive-text: "hsl(0 72% 63%)"
  plex: "hsl(40 96% 47%)"
  support: "hsl(330 70% 68%)"
typography:
  headline:
    fontFamily: "\"Source Sans 3 Variable\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: "2rem"
    letterSpacing: "-0.025em"
    fontFeature: "\"tnum\""
  title:
    fontFamily: "\"Source Sans 3 Variable\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.25
    fontFeature: "\"tnum\""
  body:
    fontFamily: "\"Source Sans 3 Variable\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: "1.25rem"
    fontFeature: "\"tnum\""
  label:
    fontFamily: "\"Source Sans 3 Variable\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: "1rem"
    letterSpacing: "0.05em"
    fontFeature: "\"tnum\""
  mono:
    fontFamily: "\"JetBrains Mono\", ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "0.75rem"
    fontWeight: 400
    lineHeight: "1rem"
    fontFeature: "\"liga\" 0, \"calt\" 0"
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "14px"
  full: "9999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "6": "24px"
  "8": "32px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "36px"
  button-outline:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    padding: "8px 16px"
    height: "36px"
  button-outline-hover:
    backgroundColor: "{colors.raised}"
  button-ghost-hover:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.foreground}"
  button-destructive:
    backgroundColor: "{colors.destructive}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
    height: "36px"
  option-unselected:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.md}"
  option-selected:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.md}"
  nav-item:
    textColor: "{colors.muted-foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "8px 12px"
  nav-item-active:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.foreground}"
  card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.xl}"
    padding: "24px"
  status-cell:
    backgroundColor: "{colors.card}"
    padding: "12px 16px"
  input:
    textColor: "{colors.foreground}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "4px 12px"
    height: "36px"
  badge:
    backgroundColor: "{colors.elevated}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.full}"
    padding: "2px 10px"
---

# Design System: Shortlist

Scope: the authenticated admin app in `web/src`. The public website in `docs/` is a separate surface with its own stylesheet and is not governed by this file.

## Overview

**Creative North Star: "The Night Shift Logbook"**

Shortlist is an operator's console read a few minutes at a time, usually the morning after a nightly run. The world is a near-black, faintly cool canvas with warm off-white text, and one amber voice. Every screen opens on the run and its privacy state, stated as a strip of plain facts, and everything below it is quieter. Density is moderate: 14px body text, tabular numerals everywhere, hairlines rather than boxes inside a panel.

Amber means "do this". It is not decoration, not "selected", and not "the pointer is here". Choice is shown by lifting a neutral surface one step and drawing a 2px amber edge on it. Depth is a single card layer on the canvas; the content inside a card is divided by 1px hairlines, never by another card. Colour beyond amber is status only: green OK, amber-orange warning, red error, and one pink reserved for the coffee link because it carries no status meaning.

User-authored emoji in row names (the literal Plex collection titles, such as "✨ Movies Picked for You") are data and render as typed. They are never used as chrome.

**Key Characteristics:**
- Dark only; warm text on cool near-black, contrast measured to AA on every surface.
- One filled-amber control per screen.
- Selected = raised neutral surface + 2px amber edge.
- One card depth, hairline dividers inside.
- Source Sans 3 with tabular numerals; mono only for code, cron, logs and filter strings, ligatures off.
- One page header shape on every screen.

## Colors

A monochrome stack of five near-black steps, warm neutral text in three strengths, one amber action colour, and status hues used only to report state.

### Primary
- **Run-Now Amber** (`primary`): the fill of the single primary action on a screen ("Run now", "Save"), the focus ring, the text caret, the 2px selected edge, and text links. Its paired dark brown (`primary-foreground`) is the only text colour allowed on it.
- **Amber Wash** (`accent`, with `accent-foreground` for text): the rare badge or callout that must draw the eye without being an action, such as a warning count chip. Used as a tint (`primary` at 10% fill, 40% border), never as a solid block.

### Neutral
- **Canvas** (`background`): the page itself.
- **Card** (`card`): the one surface level that sits on the canvas: panels, status strip cells, the mobile top bar.
- **Elevated** (`elevated`): resting controls (outline buttons, unselected segments, badges), popovers, toasts, and ghost-button hover.
- **Raised** (`raised`): the selected state, and hover on resting controls. One step above Elevated; foreground on it measures 13.6:1.
- **Hairline** (`border`) and **Control Edge** (`border-strong`): hairlines inside and around panels; the firmer edge only on things that must read as controls (outline buttons, segment groups, unselected chips).
- **Paper Text** (`foreground`), **Quiet Text** (`muted-foreground`), **Faint Text** (`faint-foreground`): primary copy, secondary lines and subtitles, and tertiary meta such as fact labels and column headings. Faint Text clears 4.5:1 only up to the Elevated surface; do not put it on anything lighter.

### Status
- **OK Green** (`success`), **Warning Amber** (`warning`), **Error Red** (`destructive`): status dots, tinted badges (15% fill), solid destructive buttons. **Error Red for words** (`destructive-text`) is the AA-safe red for text; the fill red fails AA as text.
- **Plex Gold** (`plex`): only the "Sign in with Plex" affordance, so it reads as Plex's button.
- **Coffee Pink** (`support`): only the coffee link's cup icon.

### Named Rules
**The One Amber Rule.** Exactly one filled-amber control per screen. A second action on the same screen is an outline button. Hover on neutral controls stays neutral.

**The Status Is Not Decoration Rule.** Green, warning amber and red appear only when they report a real state. Nothing is coloured to look lively.

**The Words Get Their Own Red Rule.** Red text uses `destructive-text`; `destructive` is for fills under white text.

## Typography

**Body Font:** Source Sans 3 Variable (self-hosted via @fontsource, falling back to Segoe UI, system-ui)
**Mono Font:** JetBrains Mono (ui-monospace fallback)

**Character:** A humanist sans with tabular numerals set on `body`, so counts, durations and times line up in columns. There is no separate display face; hierarchy comes from size and weight alone.

### Hierarchy
- **Headline** (600, 24px, tight tracking): the page title in the page header, once per screen.
- **Title** (600, 18px, line-height 1.25): status-strip values and panel titles; a 16px step for secondary lines of figures.
- **Body** (400 and 500, 14px / 20px): almost all copy, controls and table cells; subtitles in Quiet Text.
- **Meta** (400, 12-13px): lines under a value, timestamps, footnotes, in Quiet or Faint Text.
- **Fact label** (600, 12px, 0.05em tracking, uppercase, Faint Text): the label naming a value inside a status cell or stat tile. It labels a figure directly beneath it; it is not a heading over a section.
- **Mono** (12px, ligatures off): share-filter strings, cron expressions, log lines, code and keys only.

### Named Rules
**The Tabular Rule.** Numerals are tabular everywhere (`font-variant-numeric: tabular-nums` on `body`). Never switch a figure column back to proportional.

**The Literal Mono Rule.** Mono is for text a person might copy into another system. Ligatures are off on every mono element, because JetBrains Mono would draw `!=` as "≠" and a filter like `label!=shortlist_sarah` would no longer read as what Plex stores.

## Layout

Desktop-first, verified down to 320px. From 768px (`md`) a fixed 240px left rail holds the nav and the main column scrolls beside it, padded 32px; below 768px the rail becomes a sticky top bar (blurred card at 80%) with a slide-in drawer, and the main column is padded 16px. Content is centred and capped at 1800px; pages stay full width rather than narrowing form pages.

Every screen starts with the page header: title, one line under it, actions on the right, 24px below it. Actions wrap onto their own line rather than squeezing the subtitle. Spacing runs on a 4px base, with 8px and 12px the common gaps inside components, 16px between related blocks and 24px between sections and as card padding.

**The Run-First Rule.** A top-level screen opens on facts about the last run before anything else: the status strip comes first, any privacy callout second, everything else after.

## Elevation & Depth

Depth on the dark canvas is tonal plus one soft drop. Surfaces step up in lightness (canvas, card, elevated, raised); cards and the status strip carry the one elevation shadow, a 4% white inset highlight along the top edge with a soft dark drop beneath. Overlays (dialogs, popovers, menus, toasts, the sticky save bar) float with a heavier drop because they sit above the page, not in it. Selection is drawn with inset 2px amber edges, not with shadow.

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 1px 0 0 hsl(0 0% 100% / 0.04) inset, 0 8px 24px -12px hsl(240 40% 2% / 0.7)`): every card and the status strip; nothing else at page level.
- **Selected edge, horizontal** (`box-shadow: inset 0 -2px 0 0 hsl(var(--primary))`): segments, preset chips, filter chips.
- **Selected edge, vertical** (`box-shadow: inset 2px 0 0 0 hsl(var(--primary))`): the nav rail, the settings sub-nav, person lists.

### Named Rules
**The One Card Depth Rule.** A card never sits inside a card. Content inside a panel is divided by 1px hairlines in the border colour; a status strip draws its dividers as the border colour showing through a 1px grid gap, so they follow the cells as the grid wraps.

## Shapes

Gently rounded throughout from one base radius (10px). Cards take the larger corner (14px), the status strip, nav items and overlays 10px, buttons, inputs and segments 8px, small inner pieces 6px. Badges, status dots and avatars are fully round. Edges are 1px hairlines; no thick borders except the 2px amber selected edge.

## Components

### Buttons
Quiet, compact and exact about what they do.
- **Shape:** gently rounded (8px), 36px tall (32px small, 40px large), 14px medium-weight label, 16px icon with an 8px gap.
- **Primary:** Run-Now Amber fill with dark brown text. One per screen.
- **Outline:** Elevated fill, Control Edge border, Paper Text; hover lifts to Raised. The default for every action that is not the screen's one primary.
- **Ghost:** no fill at rest, Elevated on hover. **Destructive:** solid red fill with white text. **Link:** amber text, underline on hover.
- **States:** focus shows a 2px amber ring with a 2px canvas offset; disabled drops to 50% opacity; `loading` shows a spinner and disables. Small and icon buttons carry a transparent 44px touch target on coarse pointers that draws nothing.

### Segmented options and chips
- **Resting:** Elevated fill, Control Edge border, Quiet Text; hover lifts to Raised with Paper Text.
- **Selected:** Raised fill, Hairline border, Paper Text, 2px amber edge along the bottom. Every horizontal selected control uses this recipe; a new control picks it rather than inventing a third.

### Badges
- **Default:** fully round, Elevated fill, Control Edge border, 12px semibold. Labelling a thing stays neutral.
- **Status:** OK Green or Warning Amber text on a 15% tint of itself; solid red for destructive.
- **Accent:** the rare eye-catching badge, Amber Wash tint with a 40% amber border.

### Cards / Containers
- **Corner Style:** 14px.
- **Background:** Card surface on the canvas.
- **Shadow Strategy:** Card lift (see Elevation & Depth).
- **Border:** 1px Hairline.
- **Internal Padding:** 24px; title and subtitle stacked 6px apart.

### Inputs / Fields
- **Style:** 36px tall, transparent fill over the card, 1px input-colour border, 8px radius, 14px text, Quiet Text placeholder.
- **Focus:** 2px amber ring.
- **Disabled:** 50% opacity, not-allowed cursor. Secrets are redacted after save.

### Navigation
- **Rail:** 240px, translucent card fill with backdrop blur, a hairline on its right edge. Items are 14px medium, 12px by 8px padding, 10px radius, a 16px line icon and 10px gap, Quiet Text at rest, Elevated on hover.
- **Active:** Raised fill, Paper Text, 2px amber edge down the left. A warning dot or a count may sit at the right end of an item.
- **Footer block:** help, issue, GitHub and coffee links and the signed-in identity, separated from the main list by a hairline.
- **Mobile:** a sticky top bar with the wordmark, and the same list in a drawer that slides in from the left (0.2s).
- **Brand mark (incumbent, not a rule):** an amber-to-Plex-gold gradient tile with a line sparkle beside the "Shortlist" wordmark. The owner has not decided whether it is binding (PRODUCT.md); keep it in refinements, and propose any replacement as a choice.

### Status strip (signature)
One panel holding a row of facts: four on the dashboard (last run, next run, privacy, Plex), five on a run. Each cell is Card surface, 16px by 12px padding: a fact label with a status dot or a small icon, the value in Title type, and a 13px Quiet Text line under it. Cells are separated by hairlines drawn through a 1px grid gap; four across on desktop, two on a phone, and an odd last cell spans both columns. Never a card per fact.

### Page header (signature)
Title (Headline), one subtitle line in Quiet Text, and an action group on the right that wraps beneath when it does not fit. No icon tile: the rail already shows the page's icon, and the title aligns to the same left edge as the content below.

### Tabs
A row of 14px medium labels over a hairline, Quiet Text at rest. The shipped current tab is Paper Text with a bare 2px amber underline and no Raised surface; that differs from the selected-state recipe and is recorded as current, not as a pattern to copy. New tab-like controls use the segmented selected recipe.

### Motion
State changes are colour transitions only. Pages fade up 6px over 0.3s on entry; the drawer slides in over 0.2s; indeterminate progress sweeps over 1.2s. Every animation collapses to near zero under `prefers-reduced-motion`.

## Do's and Don'ts

### Do:
- **Do** give each screen exactly one filled-amber control and make every other action an outline button.
- **Do** mark every selection with the Raised surface and a 2px amber edge: bottom edge for horizontal controls, left edge for vertical lists.
- **Do** open every top-level screen with the page header and then facts about the last run.
- **Do** divide content inside a panel with 1px hairlines in the border colour.
- **Do** keep numerals tabular and set copyable strings (filters, cron, logs, keys) in JetBrains Mono with ligatures off.
- **Do** use `destructive-text` for red words and keep Faint Text on surfaces no lighter than Elevated.
- **Do** give small controls the invisible 44px coarse-pointer hit area.

### Don't:
- **Don't** use amber for selected states, hover, rank numbers or decoration.
- **Don't** put a card inside a card, or give each fact in a strip its own card.
- **Don't** add an icon tile to a page header.
- **Don't** use mono for prose, labels or numbers that are not code.
- **Don't** colour anything green, warning amber or red unless it reports that state.
- **Don't** use emoji as interface chrome; the only emoji on screen are the ones in user-authored row names.
- **Don't** add a light theme without a re-measured palette; every contrast figure here assumes the dark surfaces.
