"""Tell IndexNow search engines (Bing, Yandex, Seznam, Naver) that every page in the sitemap changed.

IndexNow needs no account: the key is published at the site root (``docs/<key>.txt``), and the engines
fetch it to confirm the ping came from the site's owner. Run after a website change has deployed:

    python scripts/indexnow_ping.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

SITE = "https://shortlistapp.dev"
DOCS = Path(__file__).resolve().parents[1] / "docs"


def find_key() -> str:
    """Return the IndexNow key: the one 32-hex-character ``.txt`` file in docs/ whose body is its own name."""
    for path in DOCS.glob("*.txt"):
        if re.fullmatch(r"[0-9a-f]{32}", path.stem) and path.read_text().strip() == path.stem:
            return path.stem
    raise SystemExit("no IndexNow key file found in docs/")


def main() -> int:
    """Ping api.indexnow.org with every sitemap URL and print the response code."""
    key = find_key()
    sitemap = urllib.request.urlopen(f"{SITE}/sitemap.xml", timeout=30).read().decode()
    urls = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    body = json.dumps(
        {"host": SITE.removeprefix("https://"), "key": key, "keyLocation": f"{SITE}/{key}.txt", "urlList": urls}
    )
    request = urllib.request.Request(
        "https://api.indexnow.org/indexnow",
        data=body.encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"IndexNow: {response.status} for {len(urls)} URLs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
