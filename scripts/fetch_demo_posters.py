#!/usr/bin/env python
"""Download real cover art for the demo library the docs screenshots are taken against.

The published images show a Plex home screen and a pick list, and both are mostly poster art. Drawn
placeholders made the site's own screenshots read as an unfinished mock-up, so the demo library
(`tests/fakes/fake_plex.py`) names real titles and this fetches the matching posters.

The art is NOT committed. It lands in `tests/e2e/assets/posters/` (gitignored), named by the fake
PMS rating key, and `_fake_poster` serves it only when it is there — so an ordinary test run needs
no network, no key, and no third-party art, while the committed screenshots still show the real
thing. That is the same posture the hero image already has: the repo ships the finished picture, not
a library of posters.

    TMDB_API_KEY=... python scripts/fetch_demo_posters.py

Then regenerate the images:

    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_screenshots.py -m e2e --no-cov -n0
    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_marketing_assets.py -m e2e --no-cov -n0
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fakes.fake_plex import DEMO_MOVIES, DEMO_SHOWS

OUT = Path(__file__).resolve().parents[1] / "tests" / "e2e" / "assets" / "posters"
#: w500 is the smallest TMDB bucket that still looks sharp at the 2x device scale the captures use.
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"


def _resolve(client: httpx.Client, key: str, kind: str, title: str, year: int) -> str | None:
    """That title's poster path on TMDB, or None if the search finds nothing usable."""
    path = "search/movie" if kind == "movie" else "search/tv"
    year_param = "primary_release_year" if kind == "movie" else "first_air_date_year"
    response = client.get(
        f"https://api.themoviedb.org/3/{path}",
        params={"api_key": key, "query": title, year_param: year},
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    # Falling back to an unfiltered search rather than giving up: a couple of these carry a release
    # year TMDB disagrees with by one, and a missing poster is a visible hole in a published image.
    if not results:
        response = client.get(f"https://api.themoviedb.org/3/{path}", params={"api_key": key, "query": title})
        response.raise_for_status()
        results = response.json().get("results") or []
    return next((r["poster_path"] for r in results if r.get("poster_path")), None)


def main() -> int:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if not key:
        print("set TMDB_API_KEY", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    wanted = [("movie", 100 + i, t, y) for i, (t, y) in enumerate(DEMO_MOVIES, start=1)]
    wanted += [("show", 300 + i, t, y) for i, (t, y) in enumerate(DEMO_SHOWS, start=1)]

    missing: list[str] = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for kind, rating_key, title, year in wanted:
            target = OUT / f"{rating_key}.jpg"
            if target.exists():
                continue
            poster = _resolve(client, key, kind, title, year)
            if poster is None:
                missing.append(title)
                continue
            target.write_bytes(client.get(f"{TMDB_IMAGE}{poster}").content)

    have = len(list(OUT.glob("*.jpg")))
    print(f"{have}/{len(wanted)} posters in {OUT.relative_to(Path.cwd())}")
    if missing:
        print("no poster found for: " + ", ".join(missing), file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
