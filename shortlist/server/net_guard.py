"""Guards for URLs the SERVER fetches on a user's say-so (SSRF).

Shortlist is a self-hosted Docker app, so the usual SSRF advice — block RFC1918, block loopback —
would break the entire product. Pointing at `http://192.168.1.50:32400`, `http://plex:32400`,
`http://localhost:11434` IS the normal configuration. A private-IP blocklist here would be a
security control that makes the app unusable, which is not a security control.

So this blocks only what is never a legitimate Plex / Tautulli / Radarr / Sonarr / Ollama target:

* **Cloud instance-metadata endpoints.** `169.254.169.254` and friends hand out IAM credentials to
  anything that can make an HTTP request from the instance. Nobody runs a media server there.
* **Non-HTTP schemes.** `file://`, `gopher://`, `ftp://` — protocol smuggling, local file reads.
* **Redirects**, at the call site: a permitted host can 302 to a blocked one, so the checks here are
  worthless unless the caller also refuses to follow redirects.

The result is narrow on purpose. It closes the "reach the metadata service" and "read a local file"
classes without touching the LAN addressing every self-hosted install depends on.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

# Link-local (169.254.0.0/16) carries the cloud metadata services — AWS/GCP/Azure/DO/Oracle all use
# 169.254.169.254, Alibaba 100.100.100.200. Also IPv6 link-local, which fe80::/10 covers.
_BLOCKED_NETS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fe80::/10"),
)

ALLOWED_SCHEMES = ("http", "https")


class BlockedUrl(ValueError):
    """A URL the server refuses to fetch on a user's behalf."""


def _is_blocked_ip(host: str) -> bool:
    """Is this an address we refuse to fetch, resolving a hostname if needed?

    A hostname is resolved because `metadata.google.internal` and any attacker-controlled DNS name
    can point at the metadata address. Resolution failure is NOT a block: the host may simply be
    down or not yet reachable, and refusing to save a URL because it does not resolve would make a
    broken connection unfixable from the UI.
    """
    candidates: list[str] = [host]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except OSError:
            return False  # unresolvable — let the request fail on its own terms
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if any(address in net for net in _BLOCKED_NETS):
            return True
    return False


def check_url(url: str, *, what: str = "That address") -> None:
    """Raise ``BlockedUrl`` if the server must not fetch ``url``.

    Deliberately permissive about private/loopback addresses — see the module docstring. Call this
    before any request built from a user-supplied URL, and pass ``follow_redirects=False`` on that
    request, or a permitted host can bounce you to a blocked one.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedUrl(f"{what} must start with http:// or https://")
    if not parts.hostname:
        raise BlockedUrl(f"{what} has no host")
    if _is_blocked_ip(parts.hostname):
        raise BlockedUrl(
            f"{what} points at a cloud metadata address, which Shortlist will not request. "
            "Use your media server's own address."
        )


def safe_backup_name(name: str) -> str:
    """A backup filename, or raise — never a path.

    ``config_dir / BACKUP_SUBDIR / name`` with an unvalidated ``name`` lets `../../etc/passwd` escape
    the backups directory, and restore then copies whatever it finds over the database. Owner-only and
    self-inflicted, but it costs one check to make the traversal impossible rather than merely
    unattractive.
    """
    cleaned = (name or "").strip()
    if not cleaned or cleaned != cleaned.strip("/\\") or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("backup name must be a plain filename")
    return cleaned
