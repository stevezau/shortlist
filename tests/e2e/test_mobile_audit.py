"""Mobile audit: no page may scroll sideways on a phone, and every control must be tappable.

Horizontal overflow is the mobile bug you cannot see on a laptop and users cannot un-see: the page
rocks left-right on every scroll, content sits off-screen, and nothing about it shows up in a
desktop screenshot. It is also invisible to a component test, because it only emerges when real
content meets a real viewport.

So this drives the REAL app at a phone viewport and asks the browser directly: is anything wider
than the screen, and is anything too small to tap. Findings are reported per route with the actual
offending element, not just a pass/fail.

    .venv/bin/python -m pytest tests/e2e/test_mobile_audit.py -m e2e --no-cov -n0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page

from tests.e2e.conftest import (
    OWNER_ACCOUNT_ID,
    SESSION_COOKIE,
    ShortlistApp,
    build_real_rows,
    session_serializer,
)

pytestmark = pytest.mark.e2e

#: iPhone 14/15 logical viewport — the narrowest mainstream phone still worth designing for. A page
#: that survives 390 survives nearly every Android too; 320 (iPhone SE 1st gen) is a stretch goal.
PHONE = {"width": 390, "height": 844}

#: Apple's HIG says 44x44pt, Material says 48x48dp. 40 is the floor below which a control is a
#: genuine miss-tap risk rather than merely tight — deliberately lenient so findings are real.
MIN_TAP = 40

#: How far past the viewport edge counts as overflow. Sub-pixel layout rounding routinely produces
#: fractions of a pixel; 2px keeps those out of the report without hiding anything a user would see.
SLOP = 2

# The QUESTION is "does this page scroll sideways" — the thing a user actually feels. The element
# list is diagnostics for when the answer is yes, not the verdict itself.
#
# Two ways an element can be wider than the screen without being a bug, and both must be excluded or
# the report is noise: a wide table inside an `overflow-x: auto` wrapper is a design decision, and
# anything under `overflow: hidden` (which is what Tailwind's `truncate` sets) is visually clipped —
# its bounding box is still full width, but nothing scrolls. Only unclipped overflow reaches the user.
PAGE_SCROLLS = """
() => {
  const doc = document.documentElement;
  return {
    scrollWidth: doc.scrollWidth,
    clientWidth: doc.clientWidth,
    overflowBy: doc.scrollWidth - doc.clientWidth,
  };
}
"""

FIND_OVERFLOW = """
() => {
  const docWidth = document.documentElement.clientWidth;
  const scrollable = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return true;
    }
    return false;
  };
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    // Fixed/absolute off-screen panels (a closed drawer) are parked deliberately.
    if (style.position === 'fixed' && r.right <= 0) continue;
    if (r.right <= docWidth + SLOP && r.left >= -SLOP) continue;
    if (scrollable(el)) continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.getAttribute('class') || '').slice(0, 120),
      left: Math.round(r.left),
      right: Math.round(r.right),
      width: Math.round(r.width),
      text: (el.textContent || '').trim().slice(0, 60),
    });
  }
  // Innermost first: the deepest element is the actual culprit, its ancestors just inherit the width.
  return out.slice(-8);
}
""".replace("SLOP", str(SLOP))

FIND_SMALL_TAPS = """
() => {
  const out = [];
  const sel = 'button, a[href], input, select, [role="button"], [role="tab"], [role="option"]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (getComputedStyle(el).visibility === 'hidden') continue;
    if (r.width >= MIN_TAP && r.height >= MIN_TAP) continue;
    out.push({
      tag: el.tagName.toLowerCase(),
      w: Math.round(r.width),
      h: Math.round(r.height),
      label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40),
    });
  }
  return out.slice(0, 8);
}
""".replace("MIN_TAP", str(MIN_TAP))


def _phone(browser: Browser, app: ShortlistApp):
    """A phone-sized context carrying a valid owner session.

    The session has to be injected the same way `conftest._owner_page` does it — without the cookie
    every route bounces to /login and the audit measures the login page thirteen times while
    reporting a clean bill of health.
    """
    cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
    context = browser.new_context(base_url=app.url, viewport=PHONE, is_mobile=True, has_touch=True)
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    return context


def _routes(app: ShortlistApp) -> list[tuple[str, str, str | None]]:
    """(label, path, text to wait for) for every route a user can reach after setup."""
    users = {u["username"]: u for u in app.api("GET", "/api/users").json()}
    sarah = users["sarah"]["id"]
    runs = app.api("GET", "/api/runs").json()
    run_id = runs[0]["id"]
    # Rows are `collections` in the API — the UI renamed them, the endpoint did not.
    rows = app.api("GET", "/api/collections").json()
    row_id = rows[0]["id"]
    return [
        ("dashboard", "/", "picked|watched|run"),
        ("rows", "/rows", "Picked for You"),
        ("row edit", f"/rows/{row_id}", "Schedule|Audience|Name"),
        ("row rename", f"/rows/{row_id}/rename", "name|Rename"),
        ("users", "/users", "sarah"),
        ("user detail", f"/users/{sarah}", "Because you watched|sarah"),
        ("runs", "/runs", "succeeded|ok"),
        ("run detail", f"/runs/{run_id}", "AI tokens|Summary|user"),
        ("requests", "/requests", "request"),
        ("jobs", "/jobs", "Schedules|job|Backup"),
        ("logs", "/logs", "log|level"),
        ("settings", "/settings", "Connections"),
        ("uninstall", "/settings/uninstall", "Uninstall|remove"),
    ]


def _audit(page: Page, label: str, path: str, wait: str | None) -> tuple[list, list]:
    page.goto(path)  # no networkidle: the app holds an SSE stream open, so it never goes idle
    if wait:
        try:
            page.get_by_text(__import__("re").compile(wait, __import__("re").I)).first.wait_for(timeout=8000)
        except Exception:
            page.wait_for_timeout(1200)  # audit whatever rendered; this is a layout check, not a content test
    else:
        page.wait_for_timeout(800)
    page.wait_for_timeout(400)  # let entrance/slide animations settle — mid-transform is not a layout
    scroll = page.evaluate(PAGE_SCROLLS)
    wide = page.evaluate(FIND_OVERFLOW) if scroll["overflowBy"] > SLOP else []
    return (scroll, wide), page.evaluate(FIND_SMALL_TAPS)


def _report(findings: dict[str, list], kind: str) -> str:
    lines = []
    for label, items in findings.items():
        lines.append(f"\n  {label}:")
        for item in items:
            lines.append(f"    {item}")
    return f"{kind} on {len(findings)} route(s):" + "".join(lines)


def test_no_page_scrolls_sideways_on_a_phone(browser: Browser, app: ShortlistApp) -> None:
    build_real_rows(app)  # real rows, picks and history, so pages carry real-length content
    context = _phone(browser, app)
    page = context.new_page()
    overflow: dict[str, list] = {}
    taps: dict[str, list] = {}
    try:
        for label, path, wait in _routes(app):
            (scroll, wide), small = _audit(page, label, path, wait)
            if scroll["overflowBy"] > SLOP:
                overflow[label] = [f"page scrolls {scroll['overflowBy']}px past {scroll['clientWidth']}px", *wide]
            if small:
                taps[label] = small
    finally:
        context.close()

    # Tap targets are reported but not failed on: the threshold is a judgement call and a 38px icon
    # button beside a 44px one is a design nit, not a broken page. Overflow is not a judgement call.
    if taps:
        print("\nTAP TARGETS UNDER {}px — {}".format(MIN_TAP, _report(taps, "small controls")))

    assert not overflow, _report(overflow, "HORIZONTAL OVERFLOW")


def test_the_wizard_does_not_scroll_sideways_on_a_phone(browser: Browser, fresh_app: ShortlistApp) -> None:
    """Setup is the first thing anyone sees, and plenty of people will run it from the sofa."""
    context = browser.new_context(base_url=fresh_app.url, viewport=PHONE, is_mobile=True, has_touch=True)
    page = context.new_page()
    try:
        (scroll, wide), small = _audit(page, "wizard", "/setup", "Shortlist|Welcome|Get started")
        if small:
            print(f"\nTAP TARGETS UNDER {MIN_TAP}px — wizard: {small}")
        assert scroll["overflowBy"] <= SLOP, _report({"wizard": wide}, "HORIZONTAL OVERFLOW")
    finally:
        context.close()


def test_the_mobile_drawer_opens_and_covers_the_nav(browser: Browser, app: ShortlistApp) -> None:
    """The sidebar is hidden below `md`, so the drawer is the ONLY way to navigate on a phone. If it
    fails to open, every page past the dashboard is unreachable — the worst mobile failure there is."""
    context = _phone(browser, app)
    page = context.new_page()
    try:
        page.goto("/")
        page.get_by_role("button", name="Open menu").click()
        drawer_links = page.get_by_role("link")
        drawer_links.first.wait_for(timeout=4000)
        labels = [t.strip() for t in drawer_links.all_inner_texts() if t.strip()]
        assert any("Rows" in t for t in labels), f"drawer opened without the main nav: {labels}"
        page.wait_for_timeout(500)  # the panel slides in; measuring mid-transform reports a phantom
        scroll = page.evaluate(PAGE_SCROLLS)
        assert scroll["overflowBy"] <= SLOP, _report(
            {"drawer open": [scroll, *page.evaluate(FIND_OVERFLOW)]}, "HORIZONTAL OVERFLOW"
        )
    finally:
        context.close()
