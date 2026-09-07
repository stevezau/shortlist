"""Every image the docs, the README and the Unraid templates point at must actually be there.

Nothing else catches this. Jekyll renders a missing `<img>` as a broken icon without failing the
build, GitHub does the same for the README, and the Unraid templates are read by a third party we
never run. The formats changed once already — PNG to WebP for the screenshots, JPEG for the hero —
and a single stale extension is a broken picture on the landing page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "docs" / "images"
#: `images/<name>.<ext>` however it is written — Liquid, Markdown, HTML or a raw GitHub URL.
REFERENCE = re.compile(r"images/([A-Za-z0-9._-]+\.(?:png|jpg|jpeg|webp|svg))")
SOURCES = (
    "README.md",
    "docs/_config.yml",
    "docs/_data/tour.yml",
    "docs/getting-started.md",
    "docs/_layouts/home.html",
    "unraid-templates/shortlist.xml",
    "unraid-templates/ca_profile.xml",
)


@pytest.mark.parametrize("source", SOURCES)
def test_every_referenced_image_exists(source: str) -> None:
    path = ROOT / source
    referenced = sorted(set(REFERENCE.findall(path.read_text(encoding="utf-8"))))
    missing = [name for name in referenced if not (IMAGES / name).is_file()]
    assert not missing, f"{source} points at images that are not in docs/images: {missing}"


def test_no_committed_image_is_unreferenced() -> None:
    """An orphan is a file nobody serves — usually a leftover from a format change.

    `social-preview.png` is the exception: it is named in `_config.yml` as the og:image, which this
    does count, so it needs no special case. A capture run writes several more screenshots than the
    site uses, and they should not be committed.
    """
    referenced = set()
    for source in SOURCES:
        referenced |= set(REFERENCE.findall((ROOT / source).read_text(encoding="utf-8")))
    orphans = sorted(p.name for p in IMAGES.iterdir() if p.is_file() and p.name not in referenced)
    assert not orphans, f"committed but referenced nowhere: {orphans}"
