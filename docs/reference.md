---
title: "Reference: settings, API and env vars"
description: Every Shortlist configuration key, REST API endpoint, container environment variable and default value, split into settings, the API, and how Shortlist decides things.
heading: Reference
---

The reference is three pages. Pick the one that matches what you are looking up, or find the section
you want in the lists below.

<!-- The empty `<span id="…">` before each link is the anchor that section had while the reference
     was a single page, so an old bookmark or search result still resolves here. (The exception is
     `how-settings-take-effect`, a heading the split introduced, which never had an old anchor to
     preserve.) Moving or renaming a section means changing its line below too. -->

## Settings and env vars

Container environment variables, every DB-backed settings key with its default, per-row overrides,
and what changes on Plex the moment you save one. → **[Settings reference](reference/settings.md)**

- <span id="environment-variables-container"></span>[Environment variables (container)](reference/settings.md#environment-variables-container)
- <span id="serving-from-a-subpath"></span>[Serving from a subpath](reference/settings.md#serving-from-a-subpath)
- <span id="settings-keys-db-backed-settings-ui-or-put-apisettings"></span>[Settings keys, with every default](reference/settings.md#settings-keys-db-backed-settings-ui-or-put-apisettings)
- <span id="per-row-request-overrides"></span>[Per-row request overrides](reference/settings.md#per-row-request-overrides)
- <span id="how-settings-take-effect"></span>[How settings take effect](reference/settings.md#how-settings-take-effect)
- <span id="when-a-row-appears-collectionsshow_days"></span>[When a row appears](reference/settings.md#when-a-row-appears-collectionsshow_days)
- <span id="files-under-config"></span>[Files under /config](reference/settings.md#files-under-config)

## The API

Every REST endpoint, its request and response shape, and the read-only support checks behind
**Have an issue?** → **[API reference](reference/api.md)**

- <span id="api"></span>[The whole API surface](reference/api.md)
- <span id="sign-in-and-setup"></span>[Sign-in and setup](reference/api.md#sign-in-and-setup)
- <span id="users"></span>[Users](reference/api.md#users)
- <span id="watching-account-the-owners-escape-from-seeing-everyones-rows"></span>[Watching account](reference/api.md#watching-account-the-owners-escape-from-seeing-everyones-rows)
- <span id="privacy-status"></span>[Privacy status](reference/api.md#privacy-status)
- <span id="rows"></span>[Rows](reference/api.md#rows)
- <span id="system-jobs-and-libraries"></span>[System, jobs and libraries](reference/api.md#system-jobs-and-libraries)
- <span id="runs"></span>[Runs](reference/api.md#runs)
- <span id="requests"></span>[Requests](reference/api.md#requests)
- <span id="events-and-notifications"></span>[Events and notifications](reference/api.md#events-and-notifications)
- <span id="outgoing-notifications"></span>[Outgoing notifications](reference/api.md#outgoing-notifications)
- <span id="settings-and-connections"></span>[Settings and connections](reference/api.md#settings-and-connections)
- <span id="reports-and-the-dashboard"></span>[Reports and the dashboard](reference/api.md#reports-and-the-dashboard)
- <span id="health-tokens-and-onboarding"></span>[Health, tokens and onboarding](reference/api.md#health-tokens-and-onboarding)
- <span id="support-checks-have-an-issue"></span>[Support checks ("Have an issue?")](reference/api.md#support-checks-have-an-issue)

## How Shortlist decides things

What "already watched" means, watched vs finished, why the owner sees every row, how a pick is
chosen, and how rows are kept private. → **[How Shortlist decides things](reference/concepts.md)**

- <span id="watched-titles-and-why-one-can-still-be-recommended"></span>[Watched titles, and why one can still be recommended](reference/concepts.md#watched-titles-and-why-one-can-still-be-recommended)
- <span id="what-already-watched-means-for-a-show"></span>[What "already watched" means for a show](reference/concepts.md#what-already-watched-means-for-a-show)
- <span id="watched-vs-finished"></span>[Watched vs finished](reference/concepts.md#watched-vs-finished)
- <span id="why-you-see-everyones-rows-and-the-watching-account"></span>[Why you see everyone's rows](reference/concepts.md#why-you-see-everyones-rows-and-the-watching-account)
- <span id="what-copies-your-watch-history-means-exactly"></span>[What "copies your watch history" means exactly](reference/concepts.md#what-copies-your-watch-history-means-exactly)
- <span id="the-date-problem-and-source_viewed_at"></span>[The date problem](reference/concepts.md#the-date-problem-and-source_viewed_at)
- <span id="how-a-pick-is-chosen-and-why-a-row-can-be-short"></span>[How a pick is chosen, and why a row can be short](reference/concepts.md#how-a-pick-is-chosen-and-why-a-row-can-be-short)
- <span id="how-rows-stay-private"></span>[How rows stay private](reference/concepts.md#how-rows-stay-private)

<script>
  // Every anchor the one-page reference had is a `<span id>` above, so an old link still works with
  // this script blocked: it lands on a link to wherever that section went. With the script on,
  // follow that link, so the bookmark behaves like the redirect it should have been.
  //
  // Only those spans, deliberately. This page's headings carry ids too, and site.js gives each one a
  // permalink back to its own id — following one of those would replace the URL with itself, on a
  // page that had just loaded at that same URL.
  (function () {
    var stub = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!stub || stub.tagName !== "SPAN") return;
    var link = stub.nextElementSibling;
    if (link && link.tagName === "A" && link.href) location.replace(link.href);
  })();
</script>
