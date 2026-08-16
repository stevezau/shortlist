# Changelog

All notable changes to this project are documented here. This project follows
[Conventional Commits](https://www.conventionalcommits.org/) and
[Semantic Versioning](https://semver.org/).

## [1.6.0] - 2026-08-17

### Added

- **Judge a request without leaving the inbox**
  ([#87](https://github.com/stevezau/shortlist/discussions/87)). Deciding on a queue of unfamiliar
  titles meant opening a tab per row to find out what each one was, then coming back to the bulk
  toolbar to act on them. Every waiting title now carries its synopsis, and every card has its own
  Send, Delete and Reject. The synopsis rides in the same TMDB response the poster already came
  from, so it costs nothing extra for a title a TMDB source surfaced; rows already waiting fill in
  on the next run that re-surfaces them.

- **"Watched" and "finished" are different questions, and the dashboard now asks both.** Plex's
  watched flag trips on a series' FIRST finished episode, so one episode of a sixty-episode show
  scored exactly like a whole film — on a real 47-user server, of 158 show picks credited as
  watched only 21 had been finished and 31 were a single episode, which ranked TV rows above movie
  rows for a purely structural reason. Every breakdown now reads "N watched · M finished".

### Fixed

- **A name collision took one person's whole night down with it.** A Plex collection is keyed by
  title within a library, so renaming a row onto a title that already exists there answers 409
  Conflict — and a `{top_seed}` row renames itself whenever the seed it is named after changes.
  Recorded on a 46-user server as 45 people fine and one with nothing at all, over a name. The row
  now keeps its old title for that night and still gets its titles; only the collision is
  survivable, so a dead server or a refused write still fails loudly.

- **The recent-watches feed said "watched" for a show somebody sampled one episode of.** Every
  other figure on that page already drew the distinction; this one feed never asked for it. Films
  still read "watched" — they have no middle state — and series now read "started" or "finished".

- **"8 failed" on the Jobs page led nowhere, and the Failed tab claimed nothing had failed.** The
  badge counts every failed job on record; the feed behind it could only fetch the newest hundred
  and filter those in the browser. On a real server the eight failures sat well past that window,
  so following the badge produced "No failed jobs. Nothing has given up — that's the good outcome."
  underneath a badge reading "8 failed". The badge is now a way in, and the list asks the server.

- **A run could report itself failed and then explain nothing.** The banner only appeared for a
  failure that belonged to the run itself, so a run where 45 people succeeded and one did not
  announced "Failed" and showed no reason — leaving one bad account to be found by eye among
  forty-six. Failures are now named and grouped by cause, because forty people failing one outage
  share one cause and printing it forty times buries the only fact that matters.

- **The run header said "Finishing up" for the whole per-user stretch**, and the ordering pass
  counted itself out of its own progress.

- **A local AI server can be a hosted one, and those want a key.**

### Changed

- **A UI pass over every screen.** Eight measured layout defects: the dashboard scrolled sideways
  at exactly 1280px; row names were squeezed to 32px and rendered as "Lat…"; the users and runs
  tables pushed columns — including a switch you could not press — outside their own cards on a
  phone; job names ellipsed into text you could not tell apart; the logs page opened already
  scrolled past its own heading and gave each message a third of the width it needed. All gone,
  verified at 320 through 1536.

- **Less prose in front of the controls.** On-screen explanatory text is down by roughly a third.
  The pattern removed throughout was a page-header explainer, then a section explainer, then a card
  subtitle, then a per-field paragraph — four layers before a value. Anything that prevents a real
  mistake stayed.

## [1.5.2] - 2026-08-15

### Fixed

- **One row, two names on Plex** ([#84](https://github.com/stevezau/shortlist/issues/84)). A row
  named with `{top_seed}` and set to **movies & shows** could deliver a correctly named collection to
  one library and a plain "✨ Picked for You" to the other — from the same row, for the same person,
  so both showed up side by side. Each library named the row from its OWN picks, and if the person's
  seeds were all films, the TV half had nothing to name itself after and fell back. A library still
  uses its own seed when it has one — a row spanning two libraries genuinely can follow a different
  watch in each, and its titles should say so — but it now borrows the row's other seed before giving
  up and using the default name.

- **The owner caveat is offered during setup, not pointed at**
  ([#85](https://github.com/stevezau/shortlist/issues/85)). Plex cannot hide anyone's row from the
  account that owns the server, so the owner sees everybody's on each library's shelf. Shortlist said
  so in the wizard — and then ended with "look for **You see everyone's rows** on the Users page",
  which is homework, handed out during setup, for a problem that only becomes visible in Plex days
  later. Someone was told exactly that, never went to the Users page, and reported 22 rows on their
  shelf as a bug. The wizard now asks the question where the decision is being made — *do you watch on
  this admin account?* — and opens the real "move my watching to a separate account" flow in place,
  watch-history transfer and all. Skip it and nothing changes: the note on the Users page and the
  alert are both still there, both dismissable.

- **Dismissing that note now clears the matching alert too.** Choosing "Got it — don't show this
  again" while reading the full explanation is a considered answer; the same message sitting in the
  bell afterwards was just nagging. The reverse still does not hold — clearing the bell is a light
  "seen it" and leaves the explanation on the Users page where you can find it again.

- **Shortlist no longer invents a name for a row** ([#84](https://github.com/stevezau/shortlist/issues/84)).
  A row called "Because you watched {top_seed}" needs a title the person has actually watched.
  Someone new to your server hasn't got one — so the row had no name, and Shortlist substituted a
  hardcoded "✨ Picked for You". On a 22-user server with a French row-name template, 19 of 22 people
  got that: not your words, and a claim about a watch that never happened, over a row filled with
  whatever rates highest.

  Rows are now named by you or not built. The row editor asks, right beside the name that raises the
  question: **"Name for people with nothing watched yet"**. Fill it in and they get the row under that
  name; leave it empty and they simply don't get this row until they've watched enough — often the
  right answer, since "because you watched" can't be true for them yet.

  **Shortlist tells you when this affects a row**, in the notification bell — naming the row, saying
  nothing was deleted, and linking to the field. A behaviour change nobody is told about reads as a
  new bug.

  **On upgrade, nothing is deleted** — but a `{top_seed}` row stops appearing for people with nothing
  watched until you give it that name. Their existing row stays on Plex exactly as it is, still
  private, just no longer updated; type a name and it comes back on the next rebuild. Shortlist
  deliberately does not pick one for you: the obvious candidate is your default row's own title, and
  handing two of someone's rows the same name is what makes them fight over a single collection.

- **A skipped show request now says what to do about it.** Sonarr identifies shows by their TheTVDB
  id, and Shortlist looks that up on TMDB — so when TMDB hasn't recorded one, there is no way to name
  the show to Sonarr and the request is skipped. (Guessing would be worse: the closest title match
  for "The Haunting of Bly Manor" is "The Haunting", a different and much larger series.) The reason
  on the **Requests** page used to read "no TheTVDB id for this show" — true, and no help at all
  unless you already knew what a TVDB id was. It now says TMDB has none and that adding the show in
  Sonarr yourself is the way through. A lookup that *failed* — TMDB down or slow — says something
  different again, because that one is worth waiting a night for.

- **The errors notification can be dismissed.** "N errors in the last day" counts what has already
  happened, and there is nothing to do about the past except acknowledge it — but it was pinned
  open, so the bell carried a badge for a full day with no way to clear it. It now dismisses, and the
  next error brings it straight back rather than staying hidden behind the last dismissal. The two
  alerts that stay pinned are the two describing something still true right now: runs paused, and an
  account that can see other people's rows.

## [1.5.1] - 2026-08-14

### Added

- **A way to support the project, for anyone who wants to.** A quiet "Support this project" line at
  the bottom of the sidebar, under the version, opening GitHub Sponsors. Deliberately in the chrome
  rather than among the navigation, and nothing floats over the page — people self-host to get away
  from being sold to, and anyone inclined to give goes looking rather than needing to be asked twice.

- **Somewhere to ask, rather than only somewhere to report.** GitHub Discussions is open, and Have
  an issue? now offers **Ask a question** beside **Report a bug**. Not everyone who gets stuck is
  looking at a defect — plenty are looking at Shortlist working exactly as designed and not
  understanding why, and with one button on the page those get filed as bugs. Questions go to the
  Q&A category, where an answer can be marked as the answer, so the next person searching the same
  problem finds one instead of an abandoned thread.

- **The "something else is reordering your shelf" alert now tells you how to stop it.** It used to
  say to exclude `shortlist_*`, which is a wildcard no tool's exclusion list accepts. Agregarr has
  since gained an **Exclude from Ordering (Plex Label)** field, and it matches a label exactly or as a
  prefix followed by `_` — so the single word `shortlist` covers the constant label and every
  person's own. The alert names the field and the value.

- **What a row actually cost, per row.** A run page showed one number per person — their whole
  night — so a row that took four minutes and a row that took four seconds looked identical, and the
  shared work every row depends on was billed to whichever row happened to be first. Each row now
  reports its own time and its own AI spend, with the shared setup shown separately and the pools
  that fed more than one row named. Waiting for the Plex write-lock is charged to the row that
  waited, so a slow night points at what was slow rather than at whoever was unlucky.

- **A download that says it is working.** The Logs page's **Download .zip** and Have an issue?'s
  **Download everything** were plain links: the browser fetched them in silence while the server
  gathered and zipped the logs, which takes seconds, so nothing on screen changed and the natural
  reading was that the click had missed. Both now say **Preparing…** while the file is built.

- **One label for co-managing tools.** Every row now carries a constant `shortlist` label (Plex
  stores it as `Shortlist`) alongside its per-person one. Tools that share your Recommended shelf —
  Agregarr's **Exclude from Ordering (Plex Label)**, Kometa's equivalents — take a list of labels to
  leave alone, and Shortlist's per-person labels were no use there: a 46-account server has 46 of
  them plus one per shared row, and the list goes stale the moment somebody joins or leaves. One
  entry now covers every row, for good. It hides nothing and changes nothing about who sees what —
  privacy still runs entirely off the per-person label. Existing rows pick it up the next time they
  are built, so there is nothing to run — but that first run does one extra label write per row you
  already have, and on a server whose Plex is slow to answer writes it may be a noticeably longer
  night. Once only; the log names every row as it goes.

### Fixed

- **A `{top_seed}` row no longer falls back to "✨ Picked for You" for people who have plenty of
  history** ([#84](https://github.com/stevezau/shortlist/issues/84)). A row titled "Because you
  watched…" needs a pick that came from something the person watched, and it only ever looked at the
  single best pick. Some sources suggest a title without following one — what's trending, what's
  popular on your server, a web-search find — so if one of those ranked first, the row acted as
  though the person had no history at all, with a dozen perfectly good seeded picks right behind it.
  It now takes the strongest pick that actually has a seed. That is why this was happening on
  established accounts and not just new ones.

  Still open in that issue: when there genuinely is no seed — a brand-new account — the row is named
  from a hardcoded "✨ Picked for You" that ignores your own row-name setting. Fixing that properly
  means changing what a row with no title of its own is called, and per-user rows are told apart by
  their titles, so it is a change with teeth. It is not shipping in a patch release.

- **"Remove or delete" looks like what it does.** In a row's editor it was grey text that did not
  read as a control at all, let alone one that ends in deleting a row. It now matches the Delete on
  the Rows page — red, outlined, with a bin. It still only takes you to the section at the bottom of
  the page rather than acting, because removing and deleting reach into other people's Plex and
  Cancel does not undo them.

## [1.5.0] - 2026-08-13 — withdrawn

Tagged and published for about half an hour, then pulled before anyone was told about it. Everything
in it ships in 1.5.1; the notes are kept here because the images were briefly public.


### Added

- **A row can be positioned after another Shortlist row.** Choosing where a row sits on the
  Recommended shelf offered your own library's collections, but never your other Shortlist rows — so
  "put Because you watched right after Picked for You", the obvious thing to want, could not be
  asked for (issue #81). It can now, and it is chosen as a ROW rather than as a collection: a
  per-person row is one Plex collection per person, so on a 46-account server the old picker would
  have had to list 46 identical-looking entries, and picking any one of them would have placed the
  row for that one account and nobody else. Rows are then placed in dependency order, so a row lands
  only once the row it follows is itself in position. Pointing two rows at each other, or a row at
  itself, is refused when you save it rather than silently doing nothing every night; and deleting a
  row clears the placements that pointed at it, so the rows that followed it fall back to the
  library default instead of never being placed again.

- **Run one row, from the row.** Rebuilding a single row was already possible — it was behind a
  dialog on the Runs page that made you pick, from a list, the row you were already looking at.
  **Run now** sits on each row's card and at the top of its editor, and takes you to the run it
  started so you can watch it happen. A row that is switched off can't be run, because a run doesn't
  build a disabled row, it takes it off Plex — and a button called "Run" doing that is the opposite
  of what the word promises. The editor also gains a **Runs** link to that row's own history, which
  until now only existed on the card the editor replaced. If you press Run with unsaved changes on
  screen, it says so first: a run rebuilds the row as it was last saved.

- **"Where is each row actually showing?"** — a new check on the Have an issue? page, and the only
  one that can see your OWN Home screen. Every other row-visibility check reads the share filters
  that hide a row from other people, and you, as the server owner, don't have one — so the flag that
  keeps somebody else's row off your Home was the one thing nothing could report on. It also
  separates a real fault from a Plex limitation: another person's row on your Home is always a bug,
  while their row on your Recommended shelf is what showing rows on friends' library shelves does,
  and is a setting rather than a defect.

- **Runs are now shown by row, not by person.** A run's unit of work is a row, but the run page
  listed people — so a **shared row**, built once for the whole server and belonging to nobody, had
  nowhere to appear at all. On a night when no per-person row was due, the page was 46 lines of
  "skipped" with the run's only real output, a shared row of 40 picks, nowhere on screen.

  Runs now open on a **Rows** tab showing the rows that run actually built. Each row expands to what
  it produced: a per-person row puts a person picker in front of the picks, a shared row shows them
  directly, and that is the only difference between the two kinds. Rows the run didn't touch are one
  quiet line at the bottom rather than a card each, so a run of one row looks like a run of one row.

  A row is named once, by its own name, with the libraries it delivered to beside it — a row that
  builds both Movies and TV Shows reads "✨ Picked for You · Movies · TV Shows" instead of taking one
  library's title and hiding the other. The People tab is gone: everything it did — the searchable,
  status-grouped person list and that person's own results, trace included — now sits inside the row
  they belong to, so you read a run one row at a time rather than choosing between two ways of
  slicing it.

- **A row's on/off switch is on its own editing page.** It lived only on the Rows card, and the
  editor sent you back there for it — a page change to do the one reversible thing to the row you
  already had open. It now sits beside Run now, with the same confirmation: turning a row off takes
  it off Plex for everyone who has it at the next run, and turning it back on rebuilds it. Removing
  and deleting stay at the bottom of the page, away from the rebuild controls, because they reach
  into other people's Plex and Cancel doesn't undo them — but the header now says they are there and
  takes you to them, which is the part that was missing.

- **Shared rows have a trace.** The same view a per-person row has always had, minus the per-person
  watch history a shared row doesn't use — so "why did it pick that" is answerable for a shared row
  for the first time.

- **The sidebar now names the exact build you're running.** A release shows its version
  (`Shortlist · 1.4.0`); a `:dev` build shows the branch and commit instead
  (`Shortlist · dev · 2ee14f8`), with the full commit on hover and in the debug bundle. The version
  is deliberately absent on `:dev`: it is the last RELEASED version, so on a dev build it names the
  release you came after rather than what you are running — five commits ago, in the case that
  prompted this. The commit is the only thing that tells two dev builds apart anyway, since every
  push between two releases carries the same version number, so "is my container actually on the
  fix?" had no answer short of shelling in.

### Removed

- **The Agregarr connection is gone**, and with it the Agregarr card under **Settings →
  Connections**. It existed for one job: after each run it wrote Shortlist's shelf order back into
  Agregarr so Agregarr's own half-hourly sync would reproduce it instead of undoing it. Maintaining a
  client against a private, unversioned API to stop one third-party tool fighting us over hub
  positions was not worth carrying.

  **If you had it connected**, the Recommended shelf goes back to being contested — Agregarr reorders
  it on its clock, Shortlist puts its rows back on the next run. Two ways to settle that, both
  configuration: exclude collections labelled `shortlist_*` in Agregarr (or stop its "Randomize Home
  Order" job), or set **Row placement** to "Wherever Plex puts them" and let Agregarr own the order
  outright. Your rows are still built, delivered and kept private either way — only their position on
  the shelf changes. Shortlist still tells you when it is happening: the "Something else is
  reordering your shelf" notification is unchanged, and now also names the maintained Agregarr fork.

  Your stored Agregarr address and API key are **deleted** on upgrade (migration `0067`) rather than
  left behind — the key was held encrypted and there is no longer a screen to remove it from. Past
  `run.agregarr_order` and `shelf.agregarr` events stay in your history; they record writes that
  really happened.

- **A note for Agregarr users, wherever Shortlist mentions it.** The original at `agregarr/agregarr`
  is no longer actively released, and reordering a shelf on it re-promotes collections with Plex's
  defaults — which puts other people's rows on the **server owner's** Home, the one place no share
  filter can cover. Shortlist already clears that on every run, so it is a gap between runs rather
  than a standing leak. The maintained fork at
  [bitr8/agregarr-dev](https://github.com/bitr8/agregarr-dev) (image `bitr8/agregarr`) fixes it at
  the source and is a drop-in swap. It still reorders the shelf, so the placement advice above
  applies either way.

### Changed

- **The Have an issue? page now answers the question you picked.** Choosing a problem ran one check;
  three of the cards named two or three things in their description and then opened only the first —
  "checks the queue, the schedule and the clocks" checked the queue. Each card now runs every check
  it promises, the answers stack up on the page instead of replacing one another so you can gather
  several into one report, and eleven more checks state their finding as a sentence rather than
  leaving you to read a table. A check that fails now offers its failure to copy — previously the one
  moment you had nothing to send. Filing a bug no longer needs the checks switched on at all; only
  attaching the diagnostics does.

- **The Jobs page's row schedules read as schedules.** Rows that build at the same time were joined
  into one line of comma-separated names, truncated, with the time as its subtitle and a single Edit
  button that could only take you to the rows list — there was no one row it could mean. The time
  now leads, with how many rows it builds, and each row is its own link to its own editor.

- **The dashboard's "Recently watched" says how much it is showing** and folds the rest away, rather
  than silently stopping. **"N more people watched something"** under By person is now "Show N more",
  because it was never a separate finding — it is the rest of the list above it, which now says it is
  ordered by watches.

- **The Logs page drops the paragraph explaining its own buttons.** What mattered in it — that
  everything down to DEBUG is recorded whatever level you are viewing — moved to the line at the
  bottom that already tells you the full history is in the download.

- **A "Popular on this server" row now actually shows what's popular on this server.** It was built
  the same way a personal row is: your server's most-watched titles became the _starting point_ for a
  TMDB similar-titles search and an AI web search — and that search deliberately excludes the titles
  it started from, so the most-watched titles on your server were the one thing the row could never
  contain. Every pick was a suggestion captioned "Popular on this server", which was untrue of all of
  them. It also cost around 10,000 AI tokens and a minute a night to produce.

  The row is now the count: the titles the most people on your server have actually watched, in that
  order, and each says how many ("11 people watched it"). No searching, no AI, and it builds in
  seconds. Your existing shared rows will change contents on their next run — this is the row you
  were asking for the whole time.

  A shared row's editor drops every control that only made sense for a search — where the picks come
  from, how many recent watches to build on, the AI web-search dials and **Recent releases** — because
  a tally has nothing to point them at, and a control the app ignores is worse than no control. What
  is left is what still decides the row: its libraries, how many people must have watched a title, its
  size, and its order (use **Newest first** to lean modern). Your **Don't seed** list now keeps a
  title out of a shared row entirely, rather than only stopping it being a search starting point.

### Fixed

- **Cancel stops a run.** Pressing it could leave a run writing to Plex for minutes, and a run that
  was only queued would not stop at all until the run ahead of it had finished — the code meant to
  stop it could not run until the thing it was waiting for got out of the way. A queued run is now
  finished on the spot, having done nothing. A running one stops at the next row boundary: every
  person's writes queue on a single lock, so most of them are waiting inside delivery when you press
  it, and each used to go on to write a whole row as its turn came. Measured on a 46-user server, a
  cancel that took over five minutes now takes about one — the remainder being the person whose row
  was already half-written, which is finished on purpose, and the privacy merge that hides
  everything delivered so far. Cancelling mid-retry also no longer erases the record of a collection
  that had already been written.

- **The Runs page notices when a run ends.** It had no live updates at all, so a run that finished
  while you watched kept its "Running" badge and a ticking timer until you navigated away and back —
  which made a cancel that had worked indistinguishable from one that was ignored.

- **A run that never ran says so.** Queue three runs, cancel them nine minutes later, and each
  reported "9m 26s" of work none of them had done: the duration was measured from the moment the run
  was asked for, so it was timing the queue. Runs now record when they actually started, and one
  that never got that far reads "never ran" instead of billing you for the wait.

- **The "How we picked" trace is reachable again.** It had gone with the People tab it lived in, so
  a per-person trace could not be opened from anywhere in the app. It now sits in the person's own
  panel, along with who they are, how long they took and what they cost — and shared rows keep their
  own trace beside them.

- **The watches-per-week chart tells you the numbers.** The count was on a tooltip attached to each
  bar, so on a quiet week the only place you could hover was a three-pixel sliver at the bottom of
  the chart and most of the column did nothing at all. Hovering anywhere in a week's column now
  reads it out, and the chart is dated at both ends — sixteen unlabelled bars never said _when_.

- **A downloading request updates itself.** The Sonarr/Radarr status on the Requests page was
  fetched once when the page opened and never again, so a title that finished downloading while you
  were looking at it went on saying "Searching" until you reloaded, and a title you had just sent
  showed nothing at all. It now checks while you watch and says **Checking…** while it does — and
  only while something is actually moving, so an inbox where everything has downloaded stops asking
  rather than re-reading your whole Radarr library every half minute for as long as the tab is open.

- **A Sonarr or Radarr that can't be reached says so.** A failed lookup is deliberately ignored so
  that one app being down doesn't blank the other's titles — but that left an unreachable app
  looking exactly like one that simply isn't tracking anything, and the inbox showed no badges for
  ever with nothing anywhere explaining why. Each title now says which app couldn't be reached.

- **"Where is each row actually showing?" no longer reports all-clear over a real leak.** Its
  headline sentence looked only at rows claiming the owner's Home screen, so two findings the check
  itself had already made were read straight past: a collection of ours carrying **no label** — which
  nothing can hide, so everyone on the server can see it — and rows Plex refused to answer for at
  all. Both produced the same empty result as a healthy server, and the banner said so. It now names
  an unlabelled row as the bug the server calls it, refuses to call anything clean when the reads
  failed, and — following the same rule the delete path uses — treats _every_ row reading unlabelled
  as a failed read rather than as a server full of leaks.

- **A run that is only queued no longer counts a duration up.** `started_at` is stamped the moment
  a run is _asked for_, not the moment it begins, so a run still waiting for the Plex writer lock
  ticked a stopwatch up from the button press — under a tooltip reading "Running…" beside a badge
  reading "queued". It now shows nothing until it actually starts, and the Started column says
  "queued 5m ago" rather than claiming it began then. The run's own page said "started … · still
  running" for the same reason, which — since **Run now** takes you straight there — was the first
  thing you saw after pressing it.

- **A background job waiting to be retried no longer shows how long it took.** After a failure the
  job goes back in the queue keeping the timestamps of the attempt that failed, so the Jobs page
  displayed that attempt's duration beside "Retrying" — a time for work that had not started.

- **A shared row's result is kept.** It was thrown away at the end of every run: the row was built,
  labelled and promoted on Plex, but only a one-line audit note survived — no trace, no per-library
  breakdown, no token cost, and not even the list of what it picked. Older runs can't be recovered;
  from now on it is all recorded.

- **A shared row's collections are now tracked like everyone else's.** They never entered the
  internal ledger Shortlist uses to find a row again later, which is what lets it clean up a row
  whose title changes every night.

- **Docker installs no longer report themselves as source checkouts.** The app read `GIT_SHA` and
  `GIT_BRANCH` to tell a container from a development checkout, but nothing ever set them — the image
  carried those facts only as OCI labels, which nothing running inside the container can read. The
  build now bakes them in, so the install type is right and the commit is visible.

- **Accounts that were switched off when they left your server now show as gone.** The daily roster
  sweep only compared **enabled** accounts against Plex, so anyone already turned off when they were
  deleted from Plex stayed in the Users list looking like an ordinary disabled account — with nothing
  to say why, and no way to clear them out, because **Remove** only offers itself for someone Plex no
  longer lists. Deleted Home users were the common case (one real server had five of them stuck).
  They are now badged **Left the server** and offer **Remove** like anyone else. Nothing is created
  or promoted on Plex for them — an account that was already off has no rows to take down — though
  the stale rule hiding their old row does become eligible for tidying out of everyone else's share
  filter on the next run, which is the point. The protection that refuses to act when half the server
  seems to vanish at once now covers the badge as well as the switch-off, and the two are judged
  separately, so a server carrying old departures nobody cleared can't block a real one from being
  handled today.

## [1.4.0] - 2026-08-12

### Added

- **Shortlist now tells you when it cannot make a row private.** Plex refuses a label share-filter
  for a managed account with a parental **Restriction Profile** set, so those accounts were skipped —
  on the stated grounds that such an account "sees zero collections anyway". That is true of _Younger
  Kid_ and false of _Older Kid_, which on a real server listed three collections. So for those
  accounts nothing hid other people's rows and nothing said so. Every run now checks each profiled
  account **with that account's own token** and reports what it can actually see: a dashboard alert
  that cannot be dismissed while it is true, a **"Sees N rows of others'"** badge in the Users list,
  and an explanation on the person's page. Shortlist still cannot fix it — hiding a row _is_ the
  filter Plex is refusing — so the one remedy that works (clear the Restriction Profile) is named,
  and disabling the account is explicitly called out as _not_ a fix, since that removes their own row
  rather than their view of everyone else's. (#76)

- **People who leave your Plex server are handled properly.** Losing access already switched someone
  off and removed their rows, but the Users list then showed them exactly like an account you had
  turned off yourself, and the share-filter exclude for their deleted row stayed in every other
  account's filter permanently (one real server had reached 990-character filter strings). Departed
  accounts are now badged **Left the server** and offer **Remove**, which drops their pick and run
  history and clears them out of the list. Remove deliberately keeps their original Plex share
  settings, so uninstalling Shortlist can still restore that account exactly as it was found — that
  record is the only copy. A departure is re-checked every sync, so re-inviting somebody brings them
  straight back.

- **Shortlist tells you when another tool is fighting it over the Recommended shelf.** That shelf is
  one server-wide list, so anything else that manages Plex recommendations (Kometa, agregarr,
  Plex-Meta-Manager) reorders your rows along with everything else. It is invisible from any single
  pass — Shortlist moves its rows, re-reads the shelf, confirms the new order, and is right; the other
  tool moves them back minutes later. When the same row has had to be put back three or more times in
  a day, an alert now says so, names the likely tools, and gives the two ways out: exclude
  `shortlist_*` collections in that tool, or turn off **Let Shortlist order the Recommended shelf** and
  hand the order over. Your rows are still built, delivered and kept private either way.

- **Shortlist and Agregarr stop fighting over the Recommended shelf.** If you also run
  [Agregarr](https://github.com/agregarr/agregarr), both tools arrange the same shelf and each
  undoes the other: Shortlist puts the rows up top, Agregarr re-applies its own stored order within
  half an hour, and around it goes — on one real server 35 of 91 rows were left below position 21.
  Connect Agregarr under **Settings → Connections** (its address plus the API key from Agregarr's own
  Settings → General) and the end of each run tells Agregarr where the rows ended up, so its next
  sync reproduces the shelf instead of undoing it.

  It stores the shelf **as Plex is already showing it**, never an order of its own invention, so your
  other Agregarr rows keep their order relative to each other and simply move down to make room.
  Nothing else about them is touched — not posters, visibility, titles or summaries. The connection
  is entirely optional: without it nothing changes and not one extra call is made. If Agregarr is
  unreachable the run finishes normally with a warning rather than failing, and gives up in about
  twenty seconds rather than stalling. Every attempt is recorded — `run.agregarr_order` for a nightly
  run, `shelf.agregarr` for the **Fix privacy** and **Check server** buttons — each carrying the
  before and after ordering, since Agregarr's previous order is not otherwise recoverable.

### Fixed

- **A failed run no longer clears the "can see other people's rows" alert.** The alert and the
  **Sees N rows of others'** badges are driven by the most recent run that actually checked — but
  every run recorded itself as having checked, including one that died before it got anywhere near
  the check. So a single failed run silently removed the alert, zeroed every badge and switched the
  person's page to its reassuring wording, while the exposure itself was untouched. Runs now record
  whether they got as far as looking, and only a run that did can clear a finding. The same silence
  in two other places is fixed with it: an account whose token could not be obtained was counted as
  seeing nothing rather than as unchecked, and the badge suggested turning the person off — which
  removes their own row, not their view of everyone else's, as the rest of the feature already said.

- **Rows promoted nowhere are no longer shuffled around the shelf.** A row belonging to a paused or
  disabled person sits on no surface at all, so its position is invisible to everyone — but it is
  still listed among the managed hubs, and every ordering pass moved it back into place. That was
  four pointless Plex writes per library per pass, and it made a settled shelf look contested: a
  co-managing tool rightly ignores those rows, so Shortlist alone kept moving them. A row on any
  surface — including the owner's own Home — is still placed as before.

- **Rows no longer get stranded at the bottom of the Recommended shelf.** Three separate faults, each
  enough on its own to make it unfixable. A run with no users — which is what every privacy sync is —
  came out with no libraries to order, so the whole pass silently did nothing. A per-library shelf
  anchor made the pass take its rows from the run in progress, so a run with no users had nothing to
  move and said nothing about it; it also dropped the row of anyone paused, errored or skipped. And
  the reorder itself re-issued a move for _every_ row whenever any one was misplaced (47 writes where
  19 were needed) and then reported success without ever re-reading the shelf. Ordering now happens on
  every privacy sync and on **Check and fix rows on Plex** rather than only overnight, moves only what
  is out of place, and re-reads to confirm — recording the result as unverified rather than claiming a
  shelf it did not get.

- **Dead privacy excludes are cleaned up.** A `label!=shortlist_<person>` exclude for a row that no
  longer exists is now removed from everyone's share filter — but only once **two independent checks**
  agree the row is gone: a complete collections read in which nothing carries that label, _and_
  Shortlist's own record that the person departed. Either alone can be wrong in the direction that
  un-hides a live row, so neither is trusted by itself.

- **The nightly departure sweep is now tested.** It disables people and deletes collections
  unattended, and had no test coverage at all — including the two limits that stop it acting on a
  truncated read from plex.tv (an empty roster is ignored; more than half the server appearing to
  leave at once is refused and recorded).

## [1.3.0] - 2026-08-11

### Added

- **Rows can prefer recent releases.** Ranking had no age term at all — a title's score was seed
  frequency x rating x seed weight x affinity — so a well-rated, highly-similar 1996 film beat a 2024
  one every time and rows filled with catalog titles. **Settings → Finding titles → Recent releases**
  is a weight, never a filter: an older title still reaches a row, it just has to be a better match.
  It applies to the pool CUT as well as the order, so a newer title below the line can still get in.
  **This applies to every install, existing servers included** — the default is 0.5 ("leans towards
  recent releases"), and no migration pins the old age-blind behaviour. Nothing is rewritten at
  upgrade: each row adopts it on its next refresh night, so rows shift towards newer titles over the
  following days rather than all at once. Set **Recent releases** to 0 to rank as before, globally or
  per row.

- **Changing a setting rebuilds the row instead of waiting out freshness.** Freshness is a cadence
  for suppressing churn when nothing has changed — but nothing recorded WHICH settings a row's picks
  were chosen under, so it also delayed changes made on purpose. On a real server, raising "Recent
  releases" left 36 of 42 rows redelivering byte-identical picks for up to a fortnight, which from
  the outside is indistinguishable from the setting not working. Each row now stores a fingerprint of
  the settings that decide its CONTENTS and rebuilds on a mismatch, whatever the cadence says.

- **`/api/support/surfaces`** — a read-only diagnostic that reports which Plex surfaces each row is
  actually on, included in the support bundle.

- **A run says what year each pick is and how well rated it was.** "Why is this row full of old
  films?" was unanswerable on the one page built to answer it.

- **Web search can now run entirely on your own hardware, via SearXNG.** The AI web-search source
  previously had two backends: your AI provider's own search (Claude, GPT and Gemini only) or the
  hosted Exa API. Neither suited a fully self-hosted server — a local Ollama model can't search on
  its own, so Exa was the only way to use the feature at all, and that meant a third-party account
  and sending queries off the box. **Settings → Finding titles → AI web search** now offers
  **SearXNG** as a third backend: point it at your own instance and the whole search stays local.
  It works with every AI provider, exactly as Exa does. ([#78](https://github.com/stevezau/shortlist/issues/78))

  Set it up in **Connections → Web search** — one card where you pick the backend and enter what it
  needs, replacing the separate per-vendor cards — or inline on the web-search card itself. One prerequisite,
  which the card and the **Test** button both state outright: SearXNG's JSON API is **off in a stock
  install**, so add `json` to `search.formats` in its `settings.yml` and restart. Without it SearXNG
  answers with a bare `403` that explains nothing — Shortlist translates that into the exact fix.
  A reverse-proxy login is supported via its own username/password fields, where the password is
  encrypted at rest. A login embedded in the address (`http://user:pass@host`) is refused outright,
  with a message pointing at those fields: the address itself is stored in the clear, returned by the
  API and written verbatim into the immutable `settings.change` audit event that support bundles
  export, so a password must never be allowed into it. A subpath deployment such as
  `https://example.com/searxng` is kept intact.

  Choosing a backend by name means only that backend — an unconfigured Exa or SearXNG never falls
  through to the other, which would have sent a self-hoster's queries to a paid vendor they never
  picked.

### Changed

- **The "Auto" search backend is gone.** It was the default, so it is what nearly everyone ran, and
  it meant "the provider's own search UNIONED with whichever external is configured" — a real
  behaviour that its name described not at all. You now pick exactly one backend, and it is the one
  used. Migration 0063 pins every install to what it was actually using: a configured SearXNG, else
  a configured Exa key, else the provider's own search. **A server that had both loses the combined
  search**, which did widen the candidate pool; raise a row's seed count if you want that reach back.

- **Which backend the web search uses now lives in one place.** It was briefly both on the
  Connections card and on the Finding-titles card — the same control twice, with no way to tell
  which was authoritative. Connections → Web search owns the backend and its credentials (the AI
  provider already works that way); Settings → Finding titles owns whether the source runs, and
  names the backend with a link rather than repeating the form.

- **"Test" on the provider's own web search now really searches.** Being Claude, GPT or Gemini says
  the provider offers a web-search tool; it does not say your plan or model may use it. When it may
  not, the call failed at run time, logged a warning and returned no titles — so the source quietly
  contributed nothing, every night, with nothing in the UI to say so. The Test button now performs
  one real search and tells you if the tool came back empty.

- **The run trace names every candidate and what became of it.** "What survived, and what release
  date did to it" reported counts — "40 candidates survived filtering" — which is a summary, not a
  trace: it could not answer "so why isn't X in my row". Every candidate is now listed under what
  happened to it (made the shortlist / not in your libraries / lost the cut / already watched), each
  with the rating and the release-date multiplier actually applied to it. The per-title verdicts were
  already being recorded; they were simply never shown.

- **The "not in your libraries" group says what Radarr/Sonarr did with it.** That group IS the
  request pool, and nothing on the page said so. Each title now reads "requested — added to Sonarr
  and searching" or the reason it is still waiting. Which exposed the gap behind it: the engine
  worked out per-title why a title stayed queued and then discarded it into an aggregate log line, so
  every pending row had an empty reason and the inbox's one question had no answer anywhere.

- **"How we ordered the shortlist" shows the ordering instead of describing it.** The claim an owner
  most wants checked — that one heavily-watched favourite cannot swallow the row — was the one prose
  could not settle. The rank list now marks where each source's and each watched title's turn begins.

- **Run stats say "Web searches" rather than "Exa searches".** The same counter now serves both
  external backends, so naming one vendor was simply wrong on a SearXNG server. A run's "How we
  picked" trace also records _which_ backend actually ran.

### Fixed

- **A share filter written by Plex Web is merged, not corrupted.** `parse_filter` split conditions on
  `|` and values on `,` only, but Plex Web writes a combined allow+exclude filter as
  `label=Age%200%2CAge%203&label!=X`. That parsed into one condition whose field swallowed the allow
  clause, so the existing exclude was invisible, the merge appended a second one, and the filter grew
  corrupt. This is the privacy machinery, so it is the most important fix in this release.

  **It stops the corruption; it does not undo it.** An account whose filter already carries two
  `label!=` clauses keeps both — future runs merge into the first and leave the second alone, and Plex
  Web is unhappy with the pair. If you hit this before upgrading, open **Plex → Settings → Users →
  that account → Restrictions** and delete the duplicate exclude line; Shortlist rebuilds what it
  needs on the next run. Healing them automatically means rewriting a filter we only partly wrote,
  which is how they got corrupted in the first place, so it is deliberately left as a manual step.

- **The broken-row sweep asks twice before deleting.** It is the only destructive Plex path, and an
  empty label read is indistinguishable from a genuinely unlabelled collection — so a collection is
  now re-read to confirm before removal, and a sweep where NOT ONE of our rows reads as labelled is
  treated as a failed read rather than a server full of orphans.

- **A refresh night no longer collapses a row onto one taste.** The refresh branch merged survivors
  with newcomers and then truncated by pool order, re-applying the very ordering `diversify_by_seed`
  had just defeated — so a heavily-watched title's look-alikes took the row back over.

- **Shared rows honour their display order** and stop offering dials they ignore: a shared row set to
  "Shuffled" or "Highest rated" was delivering ranking order, because the shared path never called
  the code those settings live in.

- **Cold-start rows are full size and from the right library**, and "unstarted only" is rechecked —
  three findings from the pipeline audit, all in paths whose victims are least likely to report them.

- **An exclusion written URL-encoded is no longer reported as missing.** The sharing report compared
  filter values raw while the privacy code compares them decoded, so an encoded copy of a label read
  as absent — and the one question that report exists to answer named a leak that wasn't there.

- **Cancel stops lying about what it did.** It worked on the first press and then answered "this run
  isn't currently running" to every press after, about a run that was. Asking a stopping run to stop
  is now a no-op rather than an error, and the accepted cancel is recorded on the run so a reloaded
  page still shows "Stopping…" instead of a live-looking button that can only fail.

- **A connection card says what it needs before what it is** — the part that actually blocks you was
  the easiest to skim past.

## [1.2.1] - 2026-08-07

### Added

- **A run now says what Plex ratings did to it.** Open a run → **How we picked** for a person, and
  the "Watched recently" step states the rating policy that run used in one line: off, on but nobody
  rated anything low enough, on but the account's ratings were disbelieved as tool-written, or how
  many titles a rating dropped. Until now all four looked identical — the trace listed dropped
  titles when there were any and said nothing at all when there weren't, so a run where the feature
  silently did nothing read exactly like a healthy one. The distrusted case is the one worth
  catching: a Kometa rating sync on that account means every rating is ignored, all run, invisibly.

  The line reports the setting **as it was when the run happened**, not as Settings reads today, so
  changing it later doesn't rewrite the history of a run you open a fortnight afterwards. Runs
  recorded before this release don't carry it and still show just the dropped titles.

  It also catches the half-case the account-level check is designed to let through: if a tool has
  written _some_ of someone's ratings but not enough to condemn the account, the counted ratings and
  the skipped ones are reported separately, rather than a single total that quietly speaks for values
  nothing ever looked at.

### Fixed

- **A pick's "why" said "Because you liked…" when it only ever knew what someone watched.** Every
  pick with a genre in common with its seed claimed a preference nobody had expressed — seeds are
  weighted purely by how recently something was watched, and a Plex rating can only ever _remove_ a
  seed, never mark one as liked. It now reads "Because you watched…", which is the fact we have.
  Reasons are written into each run as it happens, so runs already recorded keep the old wording.

## [1.2.0] - 2026-08-05

A minor release with **one behaviour change** — read the first item below before upgrading if any of
your rows are set to 0% already-watched, which is the default.

### Changed

- **A row set to 0% already-watched now also excludes shows someone has only STARTED.** It used to
  exclude only shows they had _finished_, where "finished" meant 80% of the episodes or a
  length-scaled floor of about three. So a series you were two episodes into was, as far as the row
  was concerned, a fresh discovery — and could be recommended straight back to you. That was
  reported, and it was doing exactly what it was told.

  Plex draws no such line: its own watched filter returns a series from its first episode. A probe of
  a real server found it returning shows as little as 1.1% watched (2 of 176), and on that server
  five of ten started shows were still eligible to be suggested to the person watching them. At 0%,
  started now means watched — the two agree.

  **Rows above 0% are unchanged.** There the percentage is still a ceiling on _finished_ titles,
  because "N% of the row" needs a definite line to mean anything. If you relied on the old
  behaviour, set the cap above 0. Rows re-pick their titles on their own refresh night, so the change
  reaches a row when it next rebuilds rather than immediately.

- **"Only series they haven't started" is now offered on a films-and-shows row**, not just a
  shows-only one. The API and the engine always accepted it there; the switch was hidden, so the
  default row most installs have could never turn it on. On a 0% row it is now a no-op — that row
  excludes started series anyway — and it still does real work on a row whose cap is above 0%.

### Added

- **"Have an issue?"** — a new page, replacing the sidebar's separate _Report a bug_ and _Copy
  diagnostics_ buttons. It runs twenty-one read-only checks against your own server and, for most
  problems, names the cause outright: which library refused someone's token, which setting actually
  applied, why a row is short, whether Plex still matches what Shortlist thinks it delivered.

  Nothing on the page changes anything — not your Plex server, not your rows, not your settings. The
  checks stay off until you switch them on and switch themselves off again after 24 hours, because
  they read share filters and per-user tokens. Every check has a **Copy for support** button, and
  what it copies is shown on screen first so a paste holds no surprises. The last section files the
  report: a pre-filled GitHub issue plus a diagnostic to attach, as a paste or a file. That
  diagnostic masks credentials, IP addresses and your server's machine id. It is a good first pass
  rather than a guarantee: logs carry whatever a dependency decided to print, so give the report a
  skim before posting it publicly. It does name the people on your server by their Plex username —
  replace those yourself if you'd rather not publish them.

  If someone reports a problem on a server you don't administer, sending them there is usually faster
  than a list of questions.

### Fixed

- **A downloaded log zip no longer carries your server's IP address or machine id.**
  `Logs → Download` calls itself "the attachment for a bug report" and had only ever removed
  credentials — so it shipped your Plex server's address on every line that recorded a call to it
  (17,234 of them in one real export) and, where a collection write was logged, the machine id that
  identifies your server. Both are now removed there and in the diagnostic report, which had the same
  gap in the log files it bundles.

  A URL keeps its scheme and port (`https://<host>:32400`) because those are the parts that answer a
  question. The live Logs view is deliberately unchanged: it renders on your own screen, where your
  server's address is what makes a line readable.

- The documentation described a `recommendations.watched_show_pct` setting that has never existed.
  The finished threshold is fixed in the engine; the docs now say so.

## [1.1.0] - 2026-08-05

A minor release: one new setting, four fixes reported by v1 users, and no breaking changes. An
existing install upgrades in place and behaves exactly as it did until you change the new setting.

### Added

- **You can now choose what someone with too little watch history gets** ([#66](https://github.com/stevezau/shortlist/issues/66)).
  Until now they always got a row of the server's highest-rated titles. That is still the default,
  but **Settings → Finding titles → When someone hasn't watched enough** can now be set to
  **Don't build their row** instead — no row is created, and any row they already have is removed,
  so "skip" means gone rather than left to go stale. It comes back on its own the night they cross
  the threshold.

  **Any row can override this in its editor**, which is the point of having it per row. A
  `{top_seed}` row ("Because you watched X") is the one worth skipping: it has no favourite to name
  itself after, so for these people it quietly falls back to a plain title. A general "Picked for
  you" row is perfectly happy holding popular titles in the meantime.

- **Where that line sits is now yours to set.** **Enough watch history** (default 10 titles) was
  fixed in the engine and unreachable; owners of small or new servers can lower it.

### Fixed

- **A run in progress no longer disagrees with itself about when it started**
  ([#67](https://github.com/stevezau/shortlist/issues/67)). "Started 8m ago" sat beside a duration
  of "10m 54s" — the two cells read the same timestamp against the same clock, but were evaluated at
  different moments. They now share one.
- **A first run no longer dies because the container started before its network did.** The one
  plex.tv read whose failure aborts a whole run had about three seconds of retry, which is not
  enough to outlast a slow network stack; it now gets about thirty. Every other read is unchanged,
  and a run still fails loudly when plex.tv is genuinely gone.
- **The Rows page now says why the admin's Collections tab lists everyone's rows.** The answer
  existed but only covered the Recommended shelf, and only while that setting was on — an owner who
  found the rows in Plex's Collections tab had nothing in front of them.
- **The "How we picked" button no longer strands mid-header** on a person whose display name is long
  enough to wrap the run card's header.

### Changed

- A muted row whose title depends on its picks (a `{top_seed}` row) is now genuinely removed from
  Plex. It could not be matched by title, so it survived every run — private, but never actually
  gone. Muting has always meant "gone"; now it is.

## [1.0.0] - 2026-08-04

First stable release. No breaking changes from `0.1.0-beta.9` — the version number is a statement
about stability, not a rewrite. An existing install upgrades in place; the migrations run on boot
after taking a pre-migration backup.

### Added

- **A "Because you watched" row can cycle its seed** instead of sitting on the newest watch for
  weeks, so the row keeps moving even when someone's viewing does not.
- **The row editor is a page**, showing what the row will actually do — and whether it is working —
  beside the settings that decide it. Rows can be renamed and deleted from there.
- **A way back from Off**, and a request filter that reaches past the 500-row inbox cap: picking a
  name under "Wanted by" now asks the SERVER, so it searches every title on file rather than the
  page that happened to load.
- **Two new row orders, for when the front of a row feels stuck** ([#63](https://github.com/stevezau/shortlist/issues/63)):
  **Just added** puts whatever is new to the row at the front, and **Taking turns** advances the
  front by one title a day so every pick gets a spell there. Both are presentation only — neither
  changes which titles a row holds, or how often it refreshes, which is still **Freshness**.
  "Newest" is now labelled **Newest released**, to keep it distinct from "Just added".
- **A rebuilt documentation site**, split into eight task-shaped guides.
- **The row editor leads with how the row is doing** — delivered, watched, runs and last built,
  across the top. Runs links to that row's own history, and counts the runs that list actually
  holds, so the number and the page behind it can never disagree.

### Fixed

- **An un-watch is noticed within the night's read** rather than waiting up to a week for the full
  re-read. Only when the read proves it covered its window — a truncated walk deletes nothing, so a
  PMS that omits `totalSize` can never be mistaken for "they un-watched everything".
- **A library removed from the server no longer counts as watched for ever.** Its cached titles and
  cursor are swept on the weekly pass. A library that is merely unshared is left alone — that
  history is still true.
- **`fetch_section` raises instead of returning an empty set** when plex.tv will not mint a token.
  Reported as "nothing watched", it made a full read wipe the section and stamp the sync a success.
- **The dashboard and Rows pages no longer scroll sideways on a phone** (134px and 184px past a
  390px screen; Rows put the Delete button out of reach entirely). Every route, the wizard, the nav
  drawer and every dialog now fit 390px, enforced by `tests/e2e/test_mobile_audit.py`.
- **The drift check's off switch now turns it off.**
- **Pages no longer wait on Plex.** The library list behind every row card was read live from the
  server on each page load, so while a job was deleting collections — one DELETE took 15.8s inside
  Plex's own write lock — every page queued behind it. It is cached now, concurrent misses collapse
  into one read, and a failed refresh serves the last good answer instead of an error.
- **Turning a row off asks first.** The next run takes that row off Plex for everyone who has it,
  which a bare switch gave no hint of. Turning one back on is unchanged — it removes nothing.
- **Rename only offers itself when the name has changed**, and now does the rename rather than
  showing you another button to press. Pressed on an unchanged name it used to rewrite every
  collection, for every person, to the name they already had.
- Warnings look like warnings, small grey text is readable, and the settings are explained in words
  a first-time user already knows.

### Changed

- The read-only Plex audit moved out of the Danger zone into Advanced — it changes nothing, and
  filing it under a destructive heading made the safest control on the page look like the riskiest.
- Renaming moved off the Rows list and into the editor, beside the name it changes.

## [0.1.0-beta.9] - 2026-08-02

### Added

- **Rows choose their own order.** Best match (the ranking, unchanged and still the default),
  Highest rated, Newest, or Shuffled. Plex only sorts a collection by release date, alphabetically,
  or by a custom order, so every one of these is applied by Shortlist and written as that custom
  order — which is what the Home row displays.
- **Highest rated can use IMDb, Trakt, Rotten Tomatoes or Metacritic** instead of TMDB, via MDBList
  (Settings → Finding titles → "Rated by", also editable straight from the row editor). TMDB needs no
  setup and costs no lookups. Without an MDBList key, or once its daily quota is spent, a row falls
  back to TMDB for its whole ordering rather than sorting half of itself on one scale and half on
  another.

### Fixed

- **A "Because you watched X" row now follows the watch it names.** Its title renders from the top
  pick, and every refresh carried that pick forward — so the row stayed named after the first thing
  that ever seeded it while newer watches quietly filled its tail. It now rebuilds around the new
  watch and renames itself when the seed moves. That template also refreshes nightly, since an
  eight-day cadence kept it naming last week's film for a week.
- **The top of a row moves again.** A refresh kept the strongest two-thirds pinned to the head, so on
  a 20-title row thirteen positions could never change however the candidates scored. Survivors and
  newcomers are now ranked together, so a better new suggestion can reach the front.
- **The library picker no longer ticks libraries a row never builds in.** An empty selection means
  "every library of this row's type"; it was drawn as every library, so a movies-only row showed its
  TV ones ticked — and touching them flipped the row to cover both, which on a one-seed row silently
  built an empty collection.

### Changed

- **The row editor is five decisions instead of nineteen.** Name, who gets it, order, schedule and
  size stand alone; artwork, what it draws on, where it appears and requests fold away, each
  captioned with its current values so a closed section still answers "is what I want in here?".
- Several settings now say what they do: "Make this a 'watch it again' row" (was "Lead with things
  they've seen"), "Watches the AI searches from" (was "Recent watches to search", and it is hidden on
  rows that do not use AI web search, where it did nothing), and freshness now says it decides _which_
  titles a row holds rather than the order they appear in.

## [0.1.0-beta.5] - 2026-07-22

### Fixed

- **The run page really does show where each pick came from now.** There were three places that
  build a pick, and the run page renders the one that was still missing provenance — a stored
  per-(row, library) breakdown, not the picks table. beta.4 fixed the renderer; the data feeding it
  was still blank.
- **Existing runs explain themselves too.** Provenance is joined onto the breakdown from the picks
  rows when a run is read, so runs recorded before this don't stay blank until they're rebuilt. A
  pick with no matching row stays blank rather than being given an invented source.

## [0.1.0-beta.4] - 2026-07-22

### Fixed

- **The run page now shows where each pick came from.** beta.3 added the "suggested by TMDB ·
  loosely related" line, but the run detail page renders its picks with its own component — so the
  line appeared on the user page and nowhere else, including the one screen people open to ask
  exactly that question.

Picks kept from an earlier run still show nothing, which is correct: those were written before
provenance was recorded, so it genuinely isn't known. They gain it the first time they are rebuilt.

## [0.1.0-beta.3] - 2026-07-22

Picks that actually resemble what you watched.

### Ranking

A beta user's row seeded by **The Pitt** — a medical drama — came back as The Sandman, Servant,
Torchwood and King & Conqueror. TMDB was not at fault: its recommendations for that show are ER,
Chicago Med, Grey's Anatomy, Code Black, The Good Doctor. Shortlist was reading the right list and
picking from the wrong end of it.

- **TMDB's ordering is no longer thrown away.** Suggestions were pooled into one bag, so "#1 closest
  match" and "#19, loosely related" arrived indistinguishable — and `/similar` (keyword matching)
  was weighted the same as `/recommendations` (what people actually watch together).
- **Ranking now asks whether a title is similar, not just well-rated.** With position discarded, the
  only thing separating candidates was TMDB's average vote — which on real data put _Traitors_, a
  reality competition show, at the top of a medical drama's row.
- **Genre coherence.** Position alone wasn't enough: TMDB tags The Pitt simply "Drama", as it does
  nearly everything it suggests. But Torchwood and The Sandman are _also_ "Sci-Fi & Fantasy", and
  that foreign genre is the whole difference.

Sources with no ranking of their own — discover, Trakt, the AI sources — are unaffected. They are
deliberate picks, not the tail of a list.

### Rows can be short now

Filling a half-empty row from the tail is how a weak association became a delivered title. Padding
now draws only from candidates that are genuinely related, so **a row may come up short** — four
titles that fit beat ten where six are filler. The run log says so, naming the closest rejected
title, so a short row reads as the filter working rather than a failure.

### Where every pick came from

Each pick records the source that surfaced it and how strongly that source vouched, shown under the
title:

```
#3  The Sandman — Because you watched The Pitt
    suggested by TMDB · loosely related
```

Nothing claims a strength it didn't measure: sources that don't rank their suggestions say only who
suggested it. The run log carries the same per row at DEBUG — every pick with its seed, source and
affinity — so a "why did it pick that?" report is answerable from a downloaded log.

### Also

- Release tags now publish `:dev` as well as `:latest` and the version tag — a tag is cut from
  `dev`, so `:dev` was being left a build behind.

## [0.1.0-beta.2] - 2026-07-22

Second beta. Mostly the things the first beta's users ran into.

### The owner is a user now

- **You get a row too.** Shortlist only ever built rows for accounts you _share with_, so on a
  one-person server it did nothing at all — plex.tv's user list never includes the account that owns
  the server ([#1]). The owner is now synced like anyone else, disabled by default so an existing
  install gains a switch rather than a row appearing unannounced. Their watch history is read from
  the PMS local account, which is named after your plex.tv **username**, not your display title.
- **The honest caveat, stated up front.** Plex cannot hide a collection from the server owner, so
  your own Home shows _every_ user's row. The app says so where it matters instead of leaving you to
  discover it.

### Say why, not just what

- **Every skip explains itself** ([#3]). "Skipped" used to be the whole message. A run now records
  the reason per person — no watch history yet, no candidates survived filtering, the row's
  libraries don't match their share — and shows it in the run detail.
- **A failed run names the account that blocked it** and what went wrong, rather than
  "promotion skipped — a privacy sync failed this run".
- **A skipped person is no longer counted as a success.** Three skipped users reported as
  "3 succeeded".

### Logs, in the app

- **A Logs view** — filter by level, search, follow live, copy, or download every log file as a zip.
  Built because diagnosing the first beta meant asking people to fish `logs.log` out of a container.
- **Redacted before you ever see it.** Plex tokens, bearer credentials and provider API keys
  (Anthropic, OpenAI including `sk-proj-`/`sk-or-v1-`, Google, xAI, Groq) are stripped from every
  line served, copied, or exported — the whole point of the view is that the output is shareable.

### Rows and users

- **Nicknames** ([#4]) — call someone what they're actually called in a row title, without touching
  their Plex username. The label never moves, so their row stays private. A Tautulli rename now
  renames the collections already on Plex instead of leaving a stale duplicate.
- **Watch history is scoped to the row's own libraries.** A row built from your 4K library was
  seeded by what you watched anywhere, so its picks could be shaped by history from a library that
  row never touches.

### One local-AI provider

- **"Local / OpenAI-compatible" replaces the separate Ollama and OpenAI-compatible options**
  ([#7]). llama.cpp, LM Studio, vLLM, LocalAI, Ollama and OpenRouter all speak the same
  `/v1/chat/completions`, so one provider with a base URL covers all of them. Existing Ollama setups
  migrate automatically. A bare host gains `/v1` for you; **Test** lists your models instead of
  making one generate, so it answers instantly.
- **It now survives the servers it exists for.** The request degrades from OpenAI's strict
  JSON-schema mode to plain JSON mode to neither, since older local builds reject the strict form
  outright; and a blank **Model** resolves to a chat model the server actually reports, rather than
  OpenAI's default (which vLLM and LM Studio reject) or the alphabetically-first name (which on a
  stock Ollama box is an embedding model that cannot chat).

### Also

- The users roster can be re-synced after setup, not only during it.
- Unraid Community Applications template and CA profile.
- CI tests only Python 3.12 — the version the image actually ships.

[#1]: https://github.com/stevezau/shortlist/issues/1
[#3]: https://github.com/stevezau/shortlist/issues/3
[#4]: https://github.com/stevezau/shortlist/issues/4
[#7]: https://github.com/stevezau/shortlist/issues/7

## [0.1.0-beta] - 2026-07-21

First public beta. Everything below ships in this release.

### Personalized rows

- **Engine** — the full nightly pipeline per user: watch history (Tautulli, with a per-user
  fallback to Plex's own history; episodes de-duplicated to distinct shows) → candidate sources →
  heuristic ranking → optional LLM curation → per-user collection delivery → merge-only
  share-filter privacy sync with snapshots.
- **Candidate sources** — TMDB similar, TMDB discover-by-taste, Trakt related titles, "AI suggests
  from your library", and **AI web search** for current/well-reviewed titles (via the curator's own
  web search or an Exa key — the latter also gives a local Ollama model web search).
- **Optional AI curator** — Anthropic / OpenAI / Google / Ollama, with a fetched model picker; or
  **None** (heuristic mode), the default. The curator only ever picks from titles verified to exist
  in your library, and writes the one-line "Because you watched X" reason.
- **Multiple rows + shared rows** — several rows per person and server-wide shared rows, each with
  its own sources, size, libraries, curation style/prompt, audience, schedule, placement, and
  poster.
- **Freshness as a cadence** — rows stay stable and refresh every N days (nightly → fortnightly),
  so a person's row isn't reshuffled every night; unchanged rows skip the Plex write entirely.
- **Row placement** — choose the Plex shelf (Home / library Recommended / both) and position, per
  row; coexists with other shelf-ordering tools.
- **Custom / AI row posters** — upload artwork or generate it from text (with `{user}` /
  `{library_name}` placeholders), reusing your AI key; cached across runs.

### Privacy & safety

- **Leak-safe row privacy** — each row is labelled `shortlist_<userslug>`; a
  `label!=shortlist_<userslug>` exclusion is merged (read-modify-write, never rebuilt) into every
  other account's share filter. Rows are swept/delivered **unpromoted**, exclusions merged, and only
  then promoted — a row is never visible before the exclusion that hides it exists.
- **Provable uninstall** — restores every user's share filters from the snapshot taken before the
  first restriction write, and deletes only `shortlist_*`-labelled collections; dry-run preview.
- **Safe mode** — `SHORTLIST_DRY_RUN=1` forces every run to dry-run (writes nothing to Plex) — try
  it against a real server first.
- **Secrets** — Plex tokens and LLM/API keys encrypted at rest (Fernet), redacted in the UI, never
  logged.

### App

- **Web app** — FastAPI backend (SQLite, APScheduler, SSE) + React SPA: an impact dashboard
  (delivered vs actually-watched hit rate), users, rows, live run activity, requests inbox, and a
  first-run onboarding wizard. Programmatic API token for automation.
- **Login with Plex** — PIN flow, owner-only sessions, CSRF-protected mutations.
- **Requests** — an approval inbox for wanted-but-missing titles, optionally auto-sent to
  Sonarr/Radarr, with a choice of rating source (TMDB, or IMDb/RT/Metacritic/Trakt via MDBList).
  Each entry shows which person and row wanted it and why; a **Sent** log records what went out.
  Rejected titles are never re-queued.
- **Packaging** — multi-arch Docker image (GHCR), compose example, Unraid template, healthcheck,
  PUID/PGID, configurable PMS timeout (`plex.timeout_s`).

### Notes

- The label-based share exclusions require PMS **≥ 1.43.2.10687** (older builds ignore the
  exclusion). The setup wizard shows the server version but never blocks a run over it.
- Collections without a `shortlist_*` label are never modified or deleted (Kometa coexistence).
- Plex cannot hide collections from the **server owner** — the owner's own Home shows every row.
