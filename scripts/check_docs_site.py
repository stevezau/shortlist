"""Check rendered Pages links, assets and browser navigation without external requests.

Run after Jekyll: python scripts/check_docs_site.py <build-directory> [--browser].
Unlike source-link tests, this checks the URLs readers receive after Liquid and Markdown render.
"""

from __future__ import annotations

import argparse
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urljoin, urlsplit


class Document(HTMLParser):
    """Collect rendered destinations and fragment targets."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        attribute = "src" if tag in {"img", "script", "source"} else "href"
        if tag in {"a", "link", "img", "script", "source"} and values.get(attribute):
            self.links.append(values[attribute])


def check_links(site: Path, origin: str = "https://shortlistapp.dev") -> list[str]:
    """Return broken internal links, including fragments and rendered image/script URLs."""
    documents = {path: Document(path.read_text()) for path in site.rglob("*.html")}
    problems = []
    for path, document in documents.items():
        route = "/" + path.relative_to(site).as_posix()
        if route.endswith("index.html"):
            route = route.removesuffix("index.html")
        for link in document.links:
            url = urlsplit(urljoin(origin + route, link))
            if url.netloc != urlsplit(origin).netloc or url.scheme not in {"http", "https"}:
                continue
            target = site / unquote(url.path).lstrip("/")
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                problems.append(f"{route}: {link} — missing destination")
            elif url.fragment and target in documents and unquote(url.fragment) not in documents[target].ids:
                problems.append(f"{route}: {link} — missing fragment")
    return sorted(set(problems))


def browser_routes(site: Path) -> list[str]:
    """Every page route the browser pass loads, sorted.

    jekyll-redirect-from's stubs for merged pages are left out: each one sends the browser to the live
    site's absolute URL, so loading it would leave the local build mid-check.
    """
    routes = []
    for page in site.rglob("index.html"):
        if 'http-equiv="refresh"' in page.read_text():
            continue
        routes.append("/" + str(page.relative_to(site)).removesuffix("index.html"))
    return sorted(routes)


def check_browser(site: Path) -> int:
    """Exercise the built site using a local server and headless Chromium."""
    from playwright.sync_api import expect, sync_playwright

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    routes = browser_routes(site)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for route in routes:
                for width in (320, 390, 1440):
                    page.set_viewport_size({"width": width, "height": 900})
                    response = page.goto(base + route, wait_until="networkidle")
                    assert response.status == 200, route
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (route, width)
                    assert page.locator("h1").count() == 1, route
                    assert not errors, errors
            for route in ("/", "/guides/interface/"):
                page.set_viewport_size({"width": 390, "height": 900})
                page.goto(base + route)
                menu = page.locator("#mobile-navigation")
                toggle = page.locator("#menu-toggle")
                expect(menu).to_be_hidden()
                toggle.focus()
                page.keyboard.press("Enter")
                expect(menu).to_be_visible()
                expect(menu.get_by_role("link", name="Getting started", exact=True)).to_be_focused()
                page.keyboard.press("Escape")
                expect(menu).to_be_hidden()
                expect(toggle).to_be_focused()
                toggle.click()
                page.mouse.click(5, 850)
                expect(menu).to_be_hidden()
                toggle.click()
                page.set_viewport_size({"width": 1440, "height": 900})
                expect(menu).to_be_hidden()
                page.locator("#search-open").click()
                page.locator("#search-input").fill("Plex")
                expect(page.locator("#search-results a").first).to_be_visible()
                page.locator("#search-close").click()
                expect(page.locator("#search-dialog")).to_be_hidden()
            fallback = browser.new_page(java_script_enabled=False, viewport={"width": 390, "height": 844})
            fallback.goto(base + "/guides/interface/")
            expect(fallback.locator("#menu-toggle")).to_be_hidden()
            fallback.locator("h1").scroll_into_view_if_needed()
            heading = fallback.locator("h1").bounding_box()
            assert heading and heading["y"] >= 0
            assert fallback.evaluate(
                """() => {
                    const h = document.querySelector('h1'), r = h.getBoundingClientRect();
                    return h.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
                }"""
            ), "No-JavaScript navigation obscures the page heading"
            fallback.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return len(routes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    site = args.site.resolve()
    if not (site / "index.html").is_file():
        parser.error("Build the site first; index.html is missing")
    problems = check_links(site)
    if problems:
        raise SystemExit("\n".join(problems))
    print("Built-site internal links, fragments and assets: passed")
    if args.browser:
        count = check_browser(site)
        print(f"{count} pages at three viewport widths; menu, focus, search and JavaScript checks: passed")


if __name__ == "__main__":
    main()
