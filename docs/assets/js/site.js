/* Shortlist docs — progressive enhancement only. Every page is fully readable
   and navigable with this file blocked; it adds the toggle, the TOC, search and
   copy buttons on top. */
(function () {
  "use strict";

  var root = document.documentElement;

  /* ---------------------------------------------------------------- theme */

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("shortlist-theme", next);
      } catch (e) {
        /* private browsing — the toggle still works for this page view */
      }
    });
  }

  /* -------------------------------------------------------- moved anchors */

  /* Sections that moved when long pages were split (October 2026). An old link such as
     /guides/rows/#seasonal-rows lands on the page that holds the section now. Only when the anchor
     is not on this page any more, so a heading that stayed put is never redirected away from. */
  var MOVED = {
    "/guides/rows/": {
      "because-you-watched-rows": "/guides/rows/what-goes-in/#because-you-watched-rows",
      "watch-it-again-rows": "/guides/rows/what-goes-in/#watch-it-again-rows",
      "when-their-finished-titles-run-out": "/guides/rows/what-goes-in/#when-their-finished-titles-run-out",
      "people-without-enough-watch-history": "/guides/rows/what-goes-in/#people-without-enough-watch-history",
      "the-order-titles-appear-in": "/guides/rows/what-goes-in/#the-order-titles-appear-in",
      "where-a-row-shows": "/guides/rows/placement/#where-a-row-shows",
      "row-placement-recommended-shelf": "/guides/rows/placement/#row-placement-recommended-shelf",
      "why-every-move-goes-to-the-bottom": "/guides/rows/placement/#why-every-move-goes-to-the-bottom",
      "if-you-also-run-agregarr": "/guides/rows/placement/#if-you-also-run-agregarr",
      "check-which-agregarr-you-are-running": "/guides/rows/placement/#check-which-agregarr-you-are-running",
      "row-posters": "/guides/rows/placement/#row-posters",
      "description-and-sort-order": "/guides/rows/placement/#description-and-sort-order",
      "seasonal-rows": "/guides/rows/seasonal/",
      "your-requests-rows": "/guides/requests/#your-requests-rows",
      "requests-on-a-row": "/guides/requests/#requests-on-a-row",
      "development-preview-editor-navigation": "/guides/rows/#editing-a-row",
    },
    "/guides/interface/": {
      "keeping-the-list-current": "/guides/people-and-sharing/#keeping-the-list-current",
      "turning-people-on-and-off": "/guides/people-and-sharing/#turning-people-on-and-off",
      "per-person-settings": "/guides/people-and-sharing/#per-person-settings",
      "sharing-and-your-watching-account": "/guides/people-and-sharing/#sharing-and-your-watching-account",
      "leaving-someones-plex-sharing-alone": "/guides/people-and-sharing/#leaving-someones-plex-sharing-alone",
      "when-someone-leaves-your-server": "/guides/people-and-sharing/#when-someone-leaves-your-server",
      "accounts-plex-restricts": "/guides/people-and-sharing/#accounts-plex-restricts",
    },
    "/reference/settings/": {
      "environment-variables-container": "/reference/environment/#environment-variables-container",
      "serving-from-a-subpath": "/reference/environment/#serving-from-a-subpath",
      "files-under-config": "/reference/environment/#files-under-config",
    },
  };
  var movedHere = MOVED[window.location.pathname.replace(/index\.html$/, "")];
  if (movedHere && window.location.hash.length > 1) {
    var oldId = decodeURIComponent(window.location.hash.slice(1));
    if (movedHere[oldId] && !document.getElementById(oldId)) {
      window.location.replace(movedHere[oldId]);
    }
  }

  /* ------------------------------------------------------- mobile sidebar */

  var burger = document.getElementById("menu-toggle");
  var mobileNav = document.getElementById("mobile-navigation");
  if (burger && mobileNav) {
    var closeMenu = function (restoreFocus) {
      mobileNav.hidden = true;
      burger.setAttribute("aria-expanded", "false");
      burger.setAttribute("aria-label", "Open navigation");
      if (restoreFocus) burger.focus();
    };
    closeMenu(false);
    burger.hidden = false;
    root.classList.add("js-mobile-nav");
    burger.addEventListener("click", function () {
      if (!mobileNav.hidden) {
        closeMenu(true);
        return;
      }
      mobileNav.hidden = false;
      burger.setAttribute("aria-expanded", "true");
      burger.setAttribute("aria-label", "Close navigation");
      mobileNav.querySelector("a").focus();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !mobileNav.hidden) {
        event.preventDefault();
        closeMenu(true);
      }
    });
    document.addEventListener("click", function (event) {
      if (!mobileNav.hidden && !mobileNav.contains(event.target) && !burger.contains(event.target)) {
        closeMenu(false);
      }
    });
    document.addEventListener("focusin", function (event) {
      if (!mobileNav.hidden && !mobileNav.contains(event.target) && event.target !== burger) {
        closeMenu(false);
      }
    });
    mobileNav.addEventListener("click", function (event) {
      if (event.target.closest("a")) closeMenu(false);
    });
    window.matchMedia("(min-width: 901px)").addEventListener("change", function (event) {
      if (event.matches) closeMenu(false);
    });
  }

  /* ------------------------------------------------------- copy to clipboard */

  var COPY_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
    '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';

  /* The install include's Copy is the one action worth counting. Only fires when the GoatCounter
     script is on the page, which head.html adds only when _config.yml names a site. */
  function countInstallCopy() {
    try {
      if (window.goatcounter && window.goatcounter.count) {
        window.goatcounter.count({ path: "install-copy", title: "Install copied", event: true });
      }
    } catch (e) {
      /* analytics must never break the copy */
    }
  }

  function attachCopy(button, getText, onCopied) {
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(getText()).then(function () {
        var label = button.querySelector(".copy__label");
        button.dataset.copied = "true";
        if (label) label.textContent = "Copied";
        if (onCopied) onCopied();
        setTimeout(function () {
          button.dataset.copied = "false";
          if (label) label.textContent = "Copy";
        }, 1800);
      });
    });
  }

  /* Prose code blocks come from markdown, so they arrive as a bare <pre>. Each gets one frame: the
     pre itself, with the copy button inside it (top right, or on its own strip on a phone). No
     language bar above it — it said "shell" over YAML often enough to be noise. */
  document.querySelectorAll(".prose pre").forEach(function (pre) {
    if (pre.closest(".cb, .codeblock")) return;
    var frame = document.createElement("div");
    frame.className = "cb";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.setAttribute("aria-label", "Copy this code");
    button.innerHTML = COPY_ICON + '<span class="copy__label">Copy</span>';
    pre.parentNode.insertBefore(frame, pre);
    frame.appendChild(pre);
    frame.appendChild(button);
  });

  document.querySelectorAll(".cb, .codeblock").forEach(function (block) {
    var button = block.querySelector(".copy");
    var pre = block.querySelector("pre");
    if (!button || !pre) return;
    attachCopy(
      button,
      function () {
        return pre.innerText;
      },
      block.hasAttribute("data-install") ? countInstallCopy : null,
    );
  });

  /* Wide markdown tables need their own scroll container or they force the
     whole page to scroll sideways on a phone. */
  document.querySelectorAll(".prose table").forEach(function (table) {
    if (table.closest(".table-scroll")) return;
    var scroller = document.createElement("div");
    scroller.className = "table-scroll";
    table.parentNode.insertBefore(scroller, table);
    scroller.appendChild(table);
  });

  /* ------------------------------------------------------------------ toc */

  var tocList = document.getElementById("toc-list");
  var toc = document.getElementById("toc");
  if (tocList && toc) {
    var headings = document.querySelectorAll(".prose h2[id], .prose h3[id]");
    if (headings.length > 2) {
      toc.hidden = false;
      headings.forEach(function (heading) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = "#" + heading.id;
        a.textContent = heading.textContent.replace(/¶|#$/, "").trim();
        a.dataset.level = heading.tagName === "H3" ? "3" : "2";
        li.appendChild(a);
        tocList.appendChild(li);
      });

      /* Highlight the heading currently at the top of the viewport. rootMargin
         pins the trigger line just below the sticky nav. */
      var links = {};
      tocList.querySelectorAll("a").forEach(function (a) {
        links[a.getAttribute("href").slice(1)] = a;
      });
      var visible = new Set();
      var observer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) visible.add(entry.target.id);
            else visible.delete(entry.target.id);
          });
          var first = null;
          headings.forEach(function (h) {
            if (first === null && visible.has(h.id)) first = h.id;
          });
          Object.keys(links).forEach(function (id) {
            links[id].classList.toggle("is-active", id === first);
          });
        },
        { rootMargin: "-80px 0px -70% 0px", threshold: 0 },
      );
      headings.forEach(function (h) {
        observer.observe(h);
      });
    }
  }

  /* -------------------------------------------------------------- reveal */

  /* Progressive enhancement, same rule as everything else in this file: .reveal
     sections are visible by default in CSS. This block is the only thing that
     can hide one, and it only hides after proving it can also un-hide — so a
     thrown error, a blocked script or a browser with no IntersectionObserver
     leaves every section at its visible default rather than stuck invisible. */
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
            obs.unobserve(entry.target); /* one-shot: not on every re-entry */
          }
        });
      },
      { threshold: 0.15, rootMargin: "0px 0px -10% 0px" },
    );
    reveals.forEach(function (el) {
      revealObserver.observe(el);
    });
  }

  /* ----------------------------------------------------------- star count */

  /* The GitHub pill and button show the repo's star count once it is known. One request per tab
     session (sessionStorage, 6 hours), and a failed request is remembered too, so a blocked or
     offline API costs one attempt rather than one per page. Without this the pill reads "GitHub". */
  var starTargets = document.querySelectorAll("[data-stars]");
  if (starTargets.length && window.fetch) {
    var STAR_KEY = "shortlist-stars";
    var STAR_TTL = 6 * 60 * 60 * 1000;
    var showStars = function (count) {
      if (typeof count !== "number") return;
      var text = count >= 1000 ? (count / 1000).toFixed(1).replace(/\.0$/, "") + "k" : String(count);
      starTargets.forEach(function (el) {
        el.textContent = text;
        var wrap = el.closest("[data-stars-wrap]") || el;
        wrap.hidden = false;
        var pill = el.closest(".nav-pill");
        if (pill) {
          pill.classList.add("has-count");
          pill.setAttribute("aria-label", "Shortlist on GitHub, " + text + " stars");
        }
      });
    };
    var remember = function (count) {
      try {
        sessionStorage.setItem(STAR_KEY, JSON.stringify({ t: Date.now(), n: count }));
      } catch (e) {
        /* private browsing: the count still shows for this page view */
      }
    };
    var cached = null;
    try {
      cached = JSON.parse(sessionStorage.getItem(STAR_KEY) || "null");
    } catch (e) {
      cached = null;
    }
    if (cached && Date.now() - cached.t < STAR_TTL) {
      showStars(cached.n);
    } else {
      fetch("https://api.github.com/repos/stevezau/shortlist", { headers: { Accept: "application/vnd.github+json" } })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (repo) {
          var count = repo && typeof repo.stargazers_count === "number" ? repo.stargazers_count : null;
          remember(count);
          showStars(count);
        })
        .catch(function () {
          remember(null);
        });
    }
  }

  /* Anchor links on prose headings, so a section can be linked to directly. */
  document
    .querySelectorAll(".prose h2[id], .prose h3[id]")
    .forEach(function (heading) {
      var a = document.createElement("a");
      a.className = "anchor";
      a.href = "#" + heading.id;
      a.textContent = "#";
      a.setAttribute("aria-label", "Link to this section");
      heading.appendChild(a);
    });

  /* --------------------------------------------------------------- search */

  var dialog = document.getElementById("search-dialog");
  var openBtn = document.getElementById("search-open");
  var closeBtn = document.getElementById("search-close");
  var input = document.getElementById("search-input");
  var results = document.getElementById("search-results");
  var index = null;
  var activeIdx = -1;

  if (
    dialog &&
    openBtn &&
    input &&
    results &&
    typeof dialog.showModal === "function"
  ) {
    var loadIndex = function () {
      if (index !== null) return Promise.resolve(index);
      return fetch(document.body.dataset.searchIndex || "search.json")
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          index = data;
          return index;
        })
        .catch(function () {
          index = [];
          return index;
        });
    };

    var openSearch = function () {
      loadIndex();
      dialog.showModal();
      input.value = "";
      render([]);
      input.focus();
    };

    openBtn.addEventListener("click", openSearch);
    if (closeBtn)
      closeBtn.addEventListener("click", function () {
        dialog.close();
      });

    document.addEventListener("keydown", function (e) {
      var typing =
        /^(input|textarea|select)$/i.test(e.target.tagName) ||
        e.target.isContentEditable;
      if (
        !dialog.open &&
        !typing &&
        (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key === "k"))
      ) {
        e.preventDefault();
        openSearch();
      }
    });

    var escapeHtml = function (s) {
      return s.replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    };

    var render = function (matches, query) {
      results.innerHTML = "";
      activeIdx = -1;
      if (!query) {
        results.innerHTML =
          '<li class="search-empty">Type to search the documentation.</li>';
        return;
      }
      if (!matches.length) {
        results.innerHTML =
          '<li class="search-empty">No matches for “' +
          escapeHtml(query) +
          "”.</li>";
        return;
      }
      matches.forEach(function (m) {
        var li = document.createElement("li");
        li.innerHTML =
          '<a href="' +
          m.url +
          '"><strong>' +
          escapeHtml(m.title) +
          "</strong><small>" +
          m.snippet +
          "</small></a>";
        results.appendChild(li);
      });
    };

    /* Deliberately simple: every term must appear somewhere in the page. With
       six pages, ranking cleverness buys nothing a substring match doesn't. */
    var search = function (query) {
      var terms = query.toLowerCase().split(/\s+/).filter(Boolean);
      if (!terms.length || !index) return [];
      return index
        .map(function (page) {
          var haystack = (
            page.title +
            " " +
            page.description +
            " " +
            page.content
          ).toLowerCase();
          if (
            !terms.every(function (t) {
              return haystack.indexOf(t) !== -1;
            })
          )
            return null;

          var at = page.content.toLowerCase().indexOf(terms[0]);
          var snippet;
          if (at === -1) {
            snippet = escapeHtml(page.description.slice(0, 150));
          } else {
            var start = Math.max(0, at - 60);
            snippet =
              (start > 0 ? "…" : "") +
              escapeHtml(page.content.slice(start, at)) +
              "<mark>" +
              escapeHtml(page.content.slice(at, at + terms[0].length)) +
              "</mark>" +
              escapeHtml(
                page.content.slice(
                  at + terms[0].length,
                  at + terms[0].length + 90,
                ),
              ) +
              "…";
          }
          var score = page.title.toLowerCase().indexOf(terms[0]) !== -1 ? 0 : 1;
          return {
            title: page.title,
            url: page.url,
            snippet: snippet,
            score: score,
          };
        })
        .filter(Boolean)
        .sort(function (a, b) {
          return a.score - b.score;
        })
        .slice(0, 8);
    };

    var run = function () {
      var query = input.value.trim();
      loadIndex().then(function () {
        render(search(query), query);
      });
    };

    input.addEventListener("input", run);

    input.addEventListener("keydown", function (e) {
      var items = results.querySelectorAll("li a");
      if (!items.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        activeIdx += e.key === "ArrowDown" ? 1 : -1;
        if (activeIdx < 0) activeIdx = items.length - 1;
        if (activeIdx >= items.length) activeIdx = 0;
        results.querySelectorAll("li").forEach(function (li, i) {
          li.classList.toggle("is-active", i === activeIdx);
        });
        items[activeIdx].scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && activeIdx >= 0) {
        e.preventDefault();
        items[activeIdx].click();
      }
    });
  } else if (openBtn) {
    openBtn.hidden = true; // no <dialog> support — don't offer a control that does nothing
  }
})();
