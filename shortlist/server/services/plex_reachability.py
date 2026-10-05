"""Why the saved Plex address did not answer — in words an owner can act on.

A Plex address that stops answering used to reach the owner as a Python exception
(`ConnectionError: HTTPSConnectionPool(host=...)`), on the run page and on the Connections card alike.
The two mistakes behind it are both invisible from that text: an address that only works over the
internet, and `localhost` typed into a container (issue #139).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests

from shortlist.engine.clients.http_retry import redact

LOOPBACK = "loopback"
DIRECT_INTERNET = "plex.direct (internet)"
DIRECT_LOCAL = "plex.direct (local)"
DIRECT = "plex.direct"
PRIVATE_IP = "private IP"
PUBLIC_IP = "public IP"
HOSTNAME = "hostname"

_DOCKER_HOST = "http://host.docker.internal:32400"
_LAN_EXAMPLE = "http://192.168.1.10:32400"

_REASONS = {
    "refused": "the connection was refused, so nothing is answering at that address",
    "dns": "that name could not be found",
    "timeout": "it did not answer in time",
    "tls": "its security certificate was not accepted",
    "unreachable": "the connection failed",
}

# How a failed lookup reads across urllib3, glibc, musl, macOS and Windows.
_DNS_SIGNS = ("nameresolutionerror", "name or service not known", "name resolution", "getaddrinfo", "nodename nor")


class PlexUnreachable(RuntimeError):
    """The Plex address did not answer. The message is the owner-facing explanation, shown as written."""


def in_container() -> bool:
    """Is Shortlist running in a container? Docker leaves `/.dockerenv`, Podman `/run/.containerenv`."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _direct_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP a plex.direct name carries as its first label: `192-168-1-50.<machine id>.plex.direct`."""
    label = host.split(".", 1)[0]
    return _ip(label.replace("-", ".")) or _ip(label.replace("-", ":"))


def address_kind(url: str) -> str:
    """What a Plex address is, for a log line or a support report — never the address itself.

    Args:
        url: A Plex server address, as saved in `plex.url`.

    Returns:
        One of this module's kind constants, e.g. `"plex.direct (internet)"`.
    """
    host = (urlsplit(url).hostname or "").lower()
    ip = _ip(host)
    if host == "localhost" or (ip is not None and ip.is_loopback):
        return LOOPBACK
    if host.endswith(".plex.direct"):
        embedded = _direct_ip(host)
        if embedded is None:
            return DIRECT
        return DIRECT_INTERNET if embedded.is_global else DIRECT_LOCAL
    if ip is not None:
        return PUBLIC_IP if ip.is_global else PRIVATE_IP
    return HOSTNAME


def describe_address(url: str) -> str:
    """A Plex address as a log line says it: `a plex.direct (internet) address (https)`."""
    return f"a {address_kind(url)} address ({urlsplit(url).scheme})"


def failure_reason(error: Exception) -> str | None:
    """Which way a connection failed: `refused`, `dns`, `timeout`, `tls` or `unreachable`.

    Reads both `requests` errors (plexapi's transport) and `httpx` ones (the setup wizard's address
    probe). The refused/DNS split is only in the message text — both libraries raise one class for it.

    Returns:
        The reason, or None when the error is not a connection failure at all.
    """
    if isinstance(error, requests.exceptions.SSLError):
        return "tls"
    if isinstance(error, requests.exceptions.Timeout | httpx.TimeoutException):
        return "timeout"
    if not isinstance(error, requests.exceptions.ConnectionError | httpx.TransportError):
        return None
    text = str(error).lower()
    if "refused" in text:
        return "refused"
    if any(sign in text for sign in _DNS_SIGNS):
        return "dns"
    if "certificate" in text or "ssl" in text:
        return "tls"
    if "timed out" in text:
        return "timeout"
    return "unreachable"


def _failed_address(error: Exception) -> str | None:
    """Where the failed request was sent (`scheme://host:port`), when the error carries its request."""
    try:
        request = error.request  # httpx raises RuntimeError here when no request was attached
    except (AttributeError, RuntimeError):
        return None
    parts = urlsplit(str(getattr(request, "url", None) or ""))
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _internet_address_note(host: str) -> str:
    return (
        f"your server's internet address, with your public IP ({_direct_ip(host)}) built into it, so it "
        "stops working when that IP changes or your internet connection is down."
    )


def explain_unreachable(url: str, error: Exception, *, plex_only: bool = False) -> str | None:
    """Turn "the Plex address did not answer" into what went wrong and what to use instead.

    Args:
        url: The Plex address that was tried.
        error: What the attempt raised.
        plex_only: The failed call was to Plex and nothing else. A failure at some OTHER host is then
            Plex's too — `url` redirected there — rather than another service's.

    Returns:
        The explanation, or None when `error` is not a connection failure to `url` — a 401, or a
        failure reaching some other service — so the caller keeps its own wording for those.
    """
    reason = failure_reason(error)
    if reason is None:
        return None
    host = _host(url)
    failed = _failed_address(error)
    if failed and _host(failed) != host:
        if not plex_only:
            return None
        # The client follows redirects, so the error names the address it was sent ON to. Without
        # both addresses here the owner sees a host they never typed (measured, issue #139).
        message = (
            f"Shortlist could not reach Plex: {url} sent Shortlist on to {failed}, "
            f"which did not work: {_REASONS[reason]}."
        )
        if address_kind(failed) == DIRECT_INTERNET:
            message += f" {failed} is {_internet_address_note(_host(failed))}"
        return redact(message)

    kind = address_kind(url)
    containerised = in_container()
    if kind == LOOPBACK and containerised:
        message = (
            f"Shortlist could not reach Plex at {url}. Shortlist runs in a container, where "
            '"localhost" means the container itself, not the machine Plex is on. '
            f"Use {_DOCKER_HOST} on Docker Desktop (Windows or Mac), or that machine's own network "
            f"address, such as {_LAN_EXAMPLE}."
        )
    elif kind == DIRECT_INTERNET:
        message = (
            f"Shortlist could not reach Plex at {url}: {_REASONS[reason]}. This is "
            f"{_internet_address_note(host)} Use an address on your own network instead, such as {_LAN_EXAMPLE}."
        )
        if containerised:
            message += f" If Plex runs on the same machine as Docker Desktop, use {_DOCKER_HOST}."
    else:
        message = f"Shortlist could not reach Plex at {url}: {_REASONS[reason]}."
        if reason == "tls" and kind != DIRECT:
            # Plex's certificate names only its plex.direct hostname, so https to anything else fails.
            message += " Plex's certificate only covers its plex.direct address, so use http:// with this one."
        else:
            message += " Check that Plex is running and that this address works from the machine Shortlist runs on."
    return redact(message)


@contextmanager
def explained(url: str, *, fix_hint: str = "") -> Iterator[None]:
    """Connect to Plex inside this — and ONLY that — and an unreachable address is raised as `PlexUnreachable`.

    Args:
        url: The Plex address being connected to.
        fix_hint: Where to change the address, appended for a reader who is not already there.

    Raises:
        PlexUnreachable: The address did not answer. Any other error passes through unchanged.
    """
    try:
        yield
    except requests.exceptions.RequestException as error:
        message = explain_unreachable(url, error, plex_only=True)
        if message is None:
            raise
        raise PlexUnreachable(f"{message}{fix_hint}") from error


def error_text(error: Exception) -> str:
    """An error as the owner sees it: our explanation as written, anything else as `Type: message`.

    Scrubbed either way — exception text can embed a tokened request URL (plex-safety rule 9).
    """
    if isinstance(error, PlexUnreachable):
        return str(error)
    return redact(f"{type(error).__name__}: {error}")
