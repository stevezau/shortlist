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

  /* ------------------------------------------------------- mobile sidebar */

  var burger = document.getElementById("menu-toggle");
  var sidebar = document.getElementById("sidebar");
  if (burger && sidebar) {
    burger.addEventListener("click", function () {
      var open = sidebar.classList.toggle("is-open");
      burger.setAttribute("aria-expanded", String(open));
    });
  } else if (burger) {
    burger.hidden = true; // the landing page has no sidebar to open
  }

  /* ------------------------------------------------------- copy to clipboard */

  function attachCopy(button, getText) {
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(getText()).then(function () {
        var label = button.querySelector(".copy__label");
        button.dataset.copied = "true";
        if (label) label.textContent = "Copied";
        setTimeout(function () {
          button.dataset.copied = "false";
          if (label) label.textContent = "Copy";
        }, 1800);
      });
    });
  }

  document.querySelectorAll(".codeblock").forEach(function (block) {
    var button = block.querySelector(".copy");
    var pre = block.querySelector("pre");
    if (button && pre)
      attachCopy(button, function () {
        return pre.innerText;
      });
  });

  /* Prose code blocks come from markdown, so their copy buttons are built here
     rather than in the template. */
  document.querySelectorAll(".prose pre").forEach(function (pre) {
    var wrapper = document.createElement("div");
    wrapper.className = "codeblock";
    var bar = document.createElement("div");
    bar.className = "codeblock__bar";
    var lang = (pre.querySelector("code") || {}).className || "";
    var match = lang.match(/language-([\w-]+)/);
    bar.innerHTML = "<span>" + (match ? match[1] : "shell") + "</span>";

    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">' +
      '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>' +
      '<span class="copy__label">Copy</span>';
    bar.appendChild(button);

    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.appendChild(bar);
    wrapper.appendChild(pre);
    attachCopy(button, function () {
      return pre.innerText;
    });
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
