"""Address and identifier redaction, shared by every artifact a user can hand to someone else.

`http_retry.redact` (aliased as `log_reader.scrub`) strips CREDENTIALS. This module strips the other
half — network addresses and this server's own identity — and lives here rather than in one API
module because there are three shareable artifacts and they must all carry the same guarantee:

* `/api/support/report.zip`   — the report plus every log file
* `/api/system/logs/download` — every log file
* the report body itself, via `support._scrub`

The machine id reached a real report through the third's sibling twice because each caller had its
own partial copy of this logic. One module, three callers, no drift.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

HOST = "<host>"
MACHINE_ID = "<machine-id>"

#: Hosts and machine ids, in the forms an exception or a log line actually carries them:
#:   https://172.16.10.240:32400/x            a bare URL
#:   host='172.16.10.240', port=32400         httpx/urllib3's connection-pool errors
#:   https://192-168-1-5.<32hex>.plex.direct  a plex.direct name, which EMBEDS the machine id
#:   https://plex.tv/api/servers/<32hex>/…    the machine id in a path
_HOST_IN_URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://([^/\s'\"]+)", re.IGNORECASE)
_HOST_KWARG = re.compile(r"\bhost\s*=\s*(['\"]?)([A-Za-z0-9._-]+)\1")

#: A 32-40 char hex id. The left boundary is NOT a plain `\b`, and that is the whole point: a log line
#: carries the id URL-encoded as `uri=server%3A%2F%2F<id>%2Fcom.plexapp…`, and `%2F` ends in `F` — a
#: hex character — so `\b` finds no boundary there and the id sails through. Two 9 MB log files in a
#: real report.zip leaked the server's machine id exactly this way. `%2F` and `%3A` are therefore
#: accepted as boundaries alongside "not alphanumeric".
_MACHINE_ID = re.compile(
    r"(?:(?<=%2F)|(?<=%3A)|(?<![0-9A-Za-z]))(?:[0-9a-f]{32,40})(?![0-9A-Za-z])",
    re.IGNORECASE,
)

#: A BARE address, no scheme and no `host=`. This is how `http_retry` logs every single PMS call
#: ("GET 172.16.10.240 -> 200"), which on a real server is tens of thousands of lines — found at
#: 17,234 occurrences in a report that the other two patterns had passed clean. Bounded so a PMS
#: version string ("1.43.3.10861") cannot match: its last part is not 1-3 digits.
_BARE_IPV4 = re.compile(r"(?<![\w.-])(?:\d{1,3}\.){3}\d{1,3}(?![\w.-])")


def shape_hosts(s: str) -> str:
    """Replace addresses with `<host>` and machine ids with `<machine-id>`, keeping scheme and port.

    `config` shapes the settings it prints, but that was only ever half of it: the same address
    arrives in every QUOTED EXCEPTION and every log line — "my Plex is unreachable" is the single most
    likely thing in a support report, and it prints `host='172.16.10.240', port=32400` verbatim. A
    `plex.direct` hostname is worse: it embeds the server's machine id, which is the identifier the
    whole privacy system keys on.

    Scheme and port survive because they carry the diagnostic value ("is it https", "is it the
    standard port"); the host itself never does.
    """

    def _url(m: re.Match[str]) -> str:
        netloc = m.group(2)
        port = netloc.rsplit(":", 1)[-1] if ":" in netloc and netloc.rsplit(":", 1)[-1].isdigit() else ""
        return f"{m.group(1)}://<host>" + (f":{port}" if port else "")

    s = _HOST_IN_URL.sub(_url, s)
    s = _HOST_KWARG.sub(r"host=\1<host>\1", s)
    # After the URL pass, so `http://1.2.3.4:32400` has already kept its port.
    s = _BARE_IPV4.sub("<host>", s)
    return _MACHINE_ID.sub("<machine-id>", s)


def redact_literals(s: str, literals: Mapping[str, str]) -> str:
    """Replace this instance's KNOWN identifiers by exact match.

    Pattern-matching alone has missed the same machine id three times, each in a different escaping.
    The exact values are not a guess, so they are replaced as literals — immune to whatever encoding
    the surrounding text happens to use — and `shape_hosts` is only the net for identifiers belonging
    to something else (another server in a plex.tv response, for instance).

    The two kinds are matched differently, and must be:

    * A **machine id** is replaced anywhere it appears, including mid-token, because the escaping is
      the whole problem — `%2F<id>%2F` and `%252F<id>%252F` both have an alphanumeric on the left.
    * A **host** is replaced only at a `[\\w.-]` boundary. A bare `str.replace` on a short hostname is
      destructive: the stock Docker Compose PMS is called `plex`, and replacing that everywhere turns
      `shortlist.engine.clients.plex_pms` into `…clients.<host>_pms`, `com.plexapp` into `com.<host>app`
      and a user named `plexfan` into `<host>fan` — which the anonymiser then fails to match, putting a
      mangled REAL username into an anonymised report.

    Args:
        s: Text bound for a client or an export.
        literals: `{value: placeholder}` from `known_identifiers`, longest value first.

    Returns:
        The text with every known identifier replaced.
    """
    for literal, placeholder in literals.items():
        if literal not in s:
            continue
        if placeholder == MACHINE_ID:
            s = s.replace(literal, placeholder)
        else:
            s = re.sub(rf"(?<![\w.-]){re.escape(literal)}(?![\w.-])", placeholder, s)
    return s


def redact_all(s: str, literals: Mapping[str, str]) -> str:
    """Literals, THEN patterns — the one canonical order, for every caller.

    The order is load-bearing and was got wrong the first time this module existed. A `plex.direct`
    hostname embeds the machine id (`192-168-1-5.<32hex>.plex.direct`), so running the patterns first
    rewrites the middle of it to `<machine-id>`; the exact hostname then no longer matches, and the
    dashed LAN IP on the front survives into the export. Literals first replaces the whole hostname
    and leaves the patterns nothing to break.
    """
    return shape_hosts(redact_literals(s, literals))


def known_identifiers(session: Session) -> dict[str, str]:
    """`{value: placeholder}` for this install's own machine ids and addresses, longest value first.

    Every `server` row, not just the first: linking a new server ADDS a row rather than replacing one,
    so `.first()` would protect the server the owner used to have and not the one they have now.
    Redacting both is the safe direction.

    Longest first so a value that is a substring of another cannot pre-empt it — which is exactly the
    `plex.direct` case, where the hostname contains the machine id.
    """
    from shortlist.server.db.models import Server

    values: dict[str, str] = {}
    for row in session.query(Server).all():
        if row.machine_id and len(row.machine_id) >= 4:
            values[row.machine_id] = MACHINE_ID
        if row.url:
            try:
                host = urlsplit(row.url).hostname or ""
            except ValueError:
                host = ""
            # Hosts are boundary-matched, so a short one is safe to carry — unlike the old length
            # floor, which dropped `pms` while admitting the far more destructive `plex`.
            if host:
                values.setdefault(host, HOST)
    return dict(sorted(values.items(), key=lambda kv: -len(kv[0])))
