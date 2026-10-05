"""Rendered output catches broken URLs that source Markdown validation cannot."""

from pathlib import Path

from scripts.check_docs_site import check_links


def test_checks_rendered_links_assets_and_fragments(tmp_path: Path) -> None:
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide/index.html").write_text('<h1 id="intro">Guide</h1>')
    (tmp_path / "index.html").write_text(
        '<a href="/guide/#intro">Valid</a>'
        '<a href="/guide.md#intro">Raw Markdown link</a>'
        '<a href="/guide/#missing">Missing heading</a>'
        '<img src="/missing.webp">'
        '<a href="https://elsewhere.test/not-local">External</a>'
    )
    failures = check_links(tmp_path)
    assert len(failures) == 3
    assert any("guide.md" in problem for problem in failures)
    assert any("missing fragment" in problem for problem in failures)
    assert any("missing.webp" in problem for problem in failures)


def test_relative_fragment_links_resolve_from_pretty_route(tmp_path: Path) -> None:
    (tmp_path / "guide").mkdir()
    (tmp_path / "index.html").write_text('<h1 id="home">Home</h1>')
    (tmp_path / "guide/index.html").write_text(
        '<h1 id="intro">Guide</h1><a href="#intro">Intro</a><a href="../#home">Home</a>'
    )
    assert check_links(tmp_path) == []


def test_browser_pass_skips_redirect_stubs(tmp_path: Path) -> None:
    """jekyll-redirect-from writes a stub that sends the browser to the live site's absolute URL.

    Loading one in the browser pass would leave the local build, and every check after it would run
    against shortlistapp.dev instead of the pages under test.
    """
    from scripts.check_docs_site import browser_routes

    (tmp_path / "guide").mkdir()
    (tmp_path / "old-page").mkdir()
    (tmp_path / "index.html").write_text("<h1>Home</h1>")
    (tmp_path / "guide/index.html").write_text("<h1>Guide</h1>")
    (tmp_path / "old-page/index.html").write_text(
        '<meta http-equiv="refresh" content="0; url=https://shortlistapp.dev/guide/"><h1>Redirecting</h1>'
    )
    assert browser_routes(tmp_path) == ["/", "/guide/"]
